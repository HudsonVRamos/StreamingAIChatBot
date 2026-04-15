const COGNITO_REGION = 'us-east-1';
const COGNITO_CLIENT_ID = '5r2hg6aag4iqbe84ldj8e0k9g';
const COGNITO_ENDPOINT = `https://cognito-idp.${COGNITO_REGION}.amazonaws.com`;

// State for multi-step auth flow
let authState = { step: 'credentials', session: null, email: null };

async function cognitoRequest(action, payload) {
    const resp = await fetch(COGNITO_ENDPOINT, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/x-amz-json-1.1',
            'X-Amz-Target': `AWSCognitoIdentityProviderService.${action}`,
        },
        body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.message || data.__type || 'Erro desconhecido');
    return data;
}

// If already logged in, go to chat
if (sessionStorage.getItem('idToken')) {
    window.location.href = 'chat.html';
}

function showError(msg) {
    const el = document.getElementById('login-error');
    el.textContent = msg;
    el.hidden = false;
}

function hideError() {
    document.getElementById('login-error').hidden = true;
}

function showSection(sectionId) {
    document.getElementById('mfa-setup-section').hidden = true;
    document.getElementById('mfa-verify-section').hidden = true;
    if (sectionId) document.getElementById(sectionId).hidden = false;
}

function completeLogin(authResult, email) {
    if (!authResult || !authResult.IdToken) {
        showError('Erro ao obter token de autenticação. Tente novamente.');
        authState = { step: 'credentials', session: null, email: null };
        showSection(null);
        document.getElementById('login-email').disabled = false;
        document.getElementById('login-password').disabled = false;
        return;
    }
    sessionStorage.setItem('idToken', authResult.IdToken);
    sessionStorage.setItem('userEmail', email);
    window.location.href = 'chat.html';
}

async function handleMfaSetup(session, email) {
    // Associate TOTP — get secret key from Cognito
    const assocResult = await cognitoRequest('AssociateSoftwareToken', {
        Session: session,
    });

    const secretCode = assocResult.SecretCode;
    authState = { step: 'mfa-setup', session: assocResult.Session, email };

    // Show QR code section
    showSection('mfa-setup-section');
    const otpauthUrl = `otpauth://totp/StreamingChatbot:${email}?secret=${secretCode}&issuer=StreamingChatbot`;
    document.getElementById('mfa-qr-container').innerHTML =
        `<img src="https://api.qrserver.com/v1/create-qr-code/?size=180x180&data=${encodeURIComponent(otpauthUrl)}" alt="QR Code" style="border-radius:8px;" />`;
    document.getElementById('mfa-secret-display').textContent = `Chave manual: ${secretCode}`;

    // Disable email/password fields
    document.getElementById('login-email').disabled = true;
    document.getElementById('login-password').disabled = true;
}

async function submitMfaSetup() {
    const code = document.getElementById('mfa-setup-code').value.trim();
    if (!/^\d{6}$/.test(code)) { showError('Digite um código de 6 dígitos'); return; }

    // Verify the TOTP token
    const verifyResult = await cognitoRequest('VerifySoftwareToken', {
        Session: authState.session,
        UserCode: code,
        FriendlyDeviceName: 'StreamingChatbot-TOTP',
    });

    if (verifyResult.Status !== 'SUCCESS') {
        showError('Código inválido. Tente novamente.');
        return;
    }

    // After VerifySoftwareToken, we need to respond to the MFA_SETUP challenge
    // But the session from VerifySoftwareToken may complete auth directly
    // or require another challenge response
    try {
        const challengeResult = await cognitoRequest('RespondToAuthChallenge', {
            ClientId: COGNITO_CLIENT_ID,
            ChallengeName: 'MFA_SETUP',
            Session: verifyResult.Session,
            ChallengeResponses: { USERNAME: authState.email },
        });

        if (challengeResult.ChallengeName === 'SOFTWARE_TOKEN_MFA') {
            authState = { step: 'mfa-verify', session: challengeResult.Session, email: authState.email };
            showSection('mfa-verify-section');
            document.getElementById('mfa-setup-section').hidden = true;
            return;
        }

        if (challengeResult.AuthenticationResult) {
            completeLogin(challengeResult.AuthenticationResult, authState.email);
            return;
        }
    } catch (_ignore) {
        // RespondToAuthChallenge may fail if session expired or flow changed
        // In that case, fall through to re-authentication
    }

    // MFA is now configured — re-authenticate to get tokens
    // Reset UI for user to enter TOTP code on fresh login
    showSection(null);
    document.getElementById('login-email').disabled = false;
    document.getElementById('login-password').disabled = false;
    document.getElementById('mfa-qr-container').innerHTML = '';
    document.getElementById('mfa-secret-display').textContent = '';
    authState = { step: 'credentials', session: null, email: null };
    showError('MFA configurado com sucesso! Faça login novamente com seu código TOTP.');
}

async function submitMfaVerify() {
    const code = document.getElementById('mfa-verify-code').value.trim();
    if (!/^\d{6}$/.test(code)) { showError('Digite um código de 6 dígitos'); return; }

    const result = await cognitoRequest('RespondToAuthChallenge', {
        ClientId: COGNITO_CLIENT_ID,
        ChallengeName: 'SOFTWARE_TOKEN_MFA',
        Session: authState.session,
        ChallengeResponses: {
            USERNAME: authState.email,
            SOFTWARE_TOKEN_MFA_CODE: code,
        },
    });

    if (result.AuthenticationResult && result.AuthenticationResult.IdToken) {
        completeLogin(result.AuthenticationResult, authState.email);
        return;
    }

    // If we still get another challenge, handle it
    if (result.ChallengeName) {
        showError(`Challenge inesperado: ${result.ChallengeName}. Tente fazer login novamente.`);
        authState = { step: 'credentials', session: null, email: null };
        showSection(null);
        document.getElementById('login-email').disabled = false;
        document.getElementById('login-password').disabled = false;
        return;
    }

    showError('Não foi possível completar o login. Tente novamente.');
}

document.getElementById('login-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    hideError();

    try {
        // Step: MFA setup — user is submitting TOTP code for first-time setup
        if (authState.step === 'mfa-setup') {
            await submitMfaSetup();
            return;
        }

        // Step: MFA verify — user is submitting TOTP code for login
        if (authState.step === 'mfa-verify') {
            await submitMfaVerify();
            return;
        }

        // Step: credentials — initial email + password
        const email = document.getElementById('login-email').value.trim();
        const password = document.getElementById('login-password').value;

        const result = await cognitoRequest('InitiateAuth', {
            ClientId: COGNITO_CLIENT_ID,
            AuthFlow: 'USER_PASSWORD_AUTH',
            AuthParameters: { USERNAME: email, PASSWORD: password },
        });

        // First login — change temporary password
        if (result.ChallengeName === 'NEW_PASSWORD_REQUIRED') {
            const newPass = prompt('Defina sua nova senha:');
            if (!newPass) return;
            const challengeResult = await cognitoRequest('RespondToAuthChallenge', {
                ClientId: COGNITO_CLIENT_ID,
                ChallengeName: 'NEW_PASSWORD_REQUIRED',
                Session: result.Session,
                ChallengeResponses: { USERNAME: email, NEW_PASSWORD: newPass },
            });

            // After password change, might need MFA setup
            if (challengeResult.ChallengeName === 'MFA_SETUP') {
                await handleMfaSetup(challengeResult.Session, email);
                return;
            }
            if (challengeResult.ChallengeName === 'SOFTWARE_TOKEN_MFA') {
                authState = { step: 'mfa-verify', session: challengeResult.Session, email };
                showSection('mfa-verify-section');
                document.getElementById('login-email').disabled = true;
                document.getElementById('login-password').disabled = true;
                return;
            }
            if (challengeResult.AuthenticationResult) {
                completeLogin(challengeResult.AuthenticationResult, email);
            }
            return;
        }

        // MFA setup required (first time with TOTP)
        if (result.ChallengeName === 'MFA_SETUP') {
            try {
                await handleMfaSetup(result.Session, email);
            } catch (setupErr) {
                showError('Erro ao iniciar configuração MFA: ' + setupErr.message);
            }
            return;
        }

        // MFA verification required (subsequent logins)
        if (result.ChallengeName === 'SOFTWARE_TOKEN_MFA') {
            authState = { step: 'mfa-verify', session: result.Session, email };
            showSection('mfa-verify-section');
            document.getElementById('login-email').disabled = true;
            document.getElementById('login-password').disabled = true;
            return;
        }

        // No MFA challenge — direct login (shouldn't happen with REQUIRED, but safe fallback)
        if (result.AuthenticationResult) {
            completeLogin(result.AuthenticationResult, email);
        }
    } catch (err) {
        showError(err.message);
    }
});
