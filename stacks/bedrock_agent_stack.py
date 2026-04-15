"""CDK Stack for Bedrock Agent with instructions and KB associations.

Creates a Bedrock Agent configured for intent classification of streaming
channel management queries. Associates KB_CONFIG and KB_LOGS knowledge bases
and configures Portuguese Brazilian instructions.

Requirements: 3.1, 3.2, 3.3, 3.4, 3.7, 3.8, 3.9
"""

from aws_cdk import (
    Stack,
    aws_bedrock as bedrock,
    aws_iam as iam,
)
from constructs import Construct

FOUNDATION_MODEL = "anthropic.claude-3-sonnet-20240229-v1:0"

AGENT_INSTRUCTIONS = """\
Você é um assistente especializado em gestão de canais de streaming MediaLive, MediaPackage, MediaTailor e CloudFront.

Regras:
1. Sempre responda em português brasileiro.
2. Classifique cada pergunta como:
   - "configuração": perguntas sobre parâmetros de canais, configurações de ad insertion, distribuições CDN, \
boas práticas, documentação técnica → consulte KB_CONFIG
   - "configuração_acao": solicitações de criação, modificação ou exclusão de canais e recursos \
(ex: "Crie um canal", "Altere o bitrate", "Configure um novo input") → consulte KB_CONFIG para templates, \
gere o JSON de configuração e encaminhe para Action_Group_Config após confirmação do usuário
   - "logs": perguntas sobre erros, falhas, incidentes, eventos (incluindo falhas de inserção de anúncios \
e erros de distribuição CDN) → consulte KB_LOGS
   - "ambos": perguntas que envolvem configuração E logs (ex: relatórios de canal) → consulte ambas as bases
   - "exportação": solicitações de exportação de listas filtradas, relatórios em arquivo ou download de dados \
(ex: "Exportar lista de canais", "Gerar CSV dos erros", "Lista de canais com low latency em JSON") → \
identifique a base de dados (KB_CONFIG, KB_LOGS ou ambas), os filtros aplicáveis e o formato de saída \
(CSV padrão ou JSON se solicitado), e encaminhe para Action_Group_Export
3. Se não encontrar informações relevantes, informe ao usuário que não possui dados suficientes.
4. Ao gerar relatórios, consolide informações de ambas as bases, incluindo dados de MediaLive, MediaPackage, \
MediaTailor e CloudFront.
5. Para intenções "configuração_acao":
   a. Consulte a KB_CONFIG para obter configurações existentes como template de referência e boas práticas aplicáveis.
   b. Gere o JSON de configuração completo e válido para a API AWS de destino.
   c. Valide a estrutura e campos obrigatórios do JSON antes de apresentar ao usuário.
   d. SEMPRE apresente o JSON completo ao usuário para revisão e solicite confirmação explícita antes de executar.
   e. NUNCA execute operações de criação, modificação ou exclusão sem confirmação explícita do usuário.
   f. Se o usuário não confirmar ou responder negativamente, cancele a operação e informe que nenhuma alteração \
foi realizada.
   g. Após confirmação, encaminhe para o Action_Group_Config para execução via Lambda_Configuradora.
6. Para intenções "exportação":
   a. Identifique a base de dados relevante: KB_CONFIG (configurações), KB_LOGS (logs/erros) ou ambas.
   b. Identifique os filtros aplicáveis a partir da consulta do usuário \
(ex: serviço, canal, período, severidade, tipo de erro, parâmetros técnicos).
   c. Identifique o formato de saída solicitado (CSV ou JSON). Se não especificado, use CSV como padrão.
   d. Encaminhe a solicitação para o Action_Group_Export com os filtros, base de dados e formato identificados.
   e. Ao receber o resultado, apresente o link de download (URL pré-assinada) e um resumo dos dados exportados \
(quantidade de registros, filtros aplicados).
   f. Se nenhum dado corresponder aos filtros, informe ao usuário que nenhum resultado foi encontrado sem gerar arquivo.\
"""

KB_CONFIG_DESCRIPTION = (
    "Use esta base para responder perguntas sobre configurações de canais, "
    "parâmetros técnicos (GOP, bitrate, codecs), configurações de inserção de "
    "anúncios (MediaTailor), distribuições CDN (CloudFront), boas práticas de "
    "streaming e documentação AWS de MediaLive/MediaPackage/MediaTailor/CloudFront. "
    "Para intenções 'configuração_acao', use esta base para buscar configurações "
    "existentes como template de referência."
)

KB_LOGS_DESCRIPTION = (
    "Use esta base para responder perguntas sobre erros, falhas, incidentes, "
    "logs de operação, diagnósticos e histórico de eventos dos canais, incluindo "
    "falhas de inserção de anúncios (MediaTailor) e erros de distribuição CDN "
    "(CloudFront)."
)


class BedrockAgentStack(Stack):
    """Stack that creates a Bedrock Agent with KB associations."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        kb_config_id: str,
        kb_logs_id: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- IAM Role for the Bedrock Agent ---
        agent_role = iam.Role(
            self,
            "BedrockAgentRole",
            assumed_by=iam.ServicePrincipal("bedrock.amazonaws.com"),
            description="IAM role for the Bedrock streaming chatbot agent",
        )

        agent_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=[
                    f"arn:aws:bedrock:{self.region}::foundation-model/{FOUNDATION_MODEL}"
                ],
            )
        )

        # Allow the agent to retrieve from the associated knowledge bases
        agent_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:Retrieve"],
                resources=[
                    f"arn:aws:bedrock:{self.region}:{self.account}:knowledge-base/*"
                ],
            )
        )

        # --- Bedrock Agent ---
        self._agent = bedrock.CfnAgent(
            self,
            "StreamingChatbotAgent",
            agent_name="StreamingChatbotAgent",
            agent_resource_role_arn=agent_role.role_arn,
            foundation_model=FOUNDATION_MODEL,
            instruction=AGENT_INSTRUCTIONS,
            description=(
                "Agente inteligente para gestão de canais de streaming "
                "MediaLive, MediaPackage, MediaTailor e CloudFront. "
                "Responde em português brasileiro."
            ),
            idle_session_ttl_in_seconds=600,
            knowledge_bases=[
                bedrock.CfnAgent.AgentKnowledgeBaseProperty(
                    knowledge_base_id=kb_config_id,
                    description=KB_CONFIG_DESCRIPTION,
                    knowledge_base_state="ENABLED",
                ),
                bedrock.CfnAgent.AgentKnowledgeBaseProperty(
                    knowledge_base_id=kb_logs_id,
                    description=KB_LOGS_DESCRIPTION,
                    knowledge_base_state="ENABLED",
                ),
            ],
        )

        # --- Agent Alias for deployment ---
        self._agent_alias = bedrock.CfnAgentAlias(
            self,
            "StreamingChatbotAgentAlias",
            agent_id=self._agent.attr_agent_id,
            agent_alias_name="live",
            description="Production alias for the streaming chatbot agent",
        )

    @property
    def agent_id(self) -> str:
        """Return the Bedrock Agent ID."""
        return self._agent.attr_agent_id

    @property
    def agent_alias_id(self) -> str:
        """Return the Bedrock Agent Alias ID."""
        return self._agent_alias.attr_agent_alias_id
