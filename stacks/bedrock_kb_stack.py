"""CDK Stack for Bedrock Knowledge Bases (KB_CONFIG and KB_LOGS).

Creates two Bedrock Knowledge Bases with S3 data sources:
- KB_CONFIG: indexes enriched configurations from kb-config/ prefix
- KB_LOGS: indexes structured events from kb-logs/ prefix

Both use Amazon Titan Embeddings V2 and the default Bedrock-managed vector store.

Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 6.1, 6.2, 6.3
"""

from aws_cdk import (
    Stack,
    aws_bedrock as bedrock,
    aws_iam as iam,
    aws_s3 as s3,
)
from constructs import Construct

TITAN_EMBED_V2_ARN = "arn:aws:bedrock:{region}::foundation-model/amazon.titan-embed-text-v2:0"


class BedrockKbStack(Stack):
    """Stack that creates Bedrock Knowledge Bases for config and logs."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        kb_config_bucket: s3.IBucket,
        kb_logs_bucket: s3.IBucket,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        embedding_model_arn = TITAN_EMBED_V2_ARN.format(region=self.region)

        # --- IAM Role for KB_CONFIG ---
        kb_config_role = iam.Role(
            self,
            "KBConfigRole",
            assumed_by=iam.ServicePrincipal("bedrock.amazonaws.com"),
            description="Role for Bedrock KB_CONFIG to access S3 and embeddings",
        )

        kb_config_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=[
                    f"arn:aws:bedrock:{self.region}::foundation-model/amazon.titan-embed-text-v2:0"
                ],
            )
        )
        kb_config_bucket.grant_read(kb_config_role)

        # --- IAM Role for KB_LOGS ---
        kb_logs_role = iam.Role(
            self,
            "KBLogsRole",
            assumed_by=iam.ServicePrincipal("bedrock.amazonaws.com"),
            description="Role for Bedrock KB_LOGS to access S3 and embeddings",
        )
        kb_logs_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=[
                    f"arn:aws:bedrock:{self.region}::foundation-model/amazon.titan-embed-text-v2:0"
                ],
            )
        )
        kb_logs_bucket.grant_read(kb_logs_role)

        # --- KB_CONFIG Knowledge Base ---
        self._kb_config = bedrock.CfnKnowledgeBase(
            self,
            "KBConfig",
            name="KB_CONFIG",
            description=(
                "Base de conhecimento de configurações de canais MediaLive, "
                "MediaPackage, MediaTailor e CloudFront, documentação técnica "
                "e boas práticas de streaming."
            ),
            role_arn=kb_config_role.role_arn,
            knowledge_base_configuration=bedrock.CfnKnowledgeBase.KnowledgeBaseConfigurationProperty(
                type="VECTOR",
                vector_knowledge_base_configuration=bedrock.CfnKnowledgeBase.VectorKnowledgeBaseConfigurationProperty(
                    embedding_model_arn=embedding_model_arn,
                ),
            ),
            storage_configuration=bedrock.CfnKnowledgeBase.StorageConfigurationProperty(
                type="S3_VECTORS",
                s3_vectors_configuration=bedrock.CfnKnowledgeBase.S3VectorsConfigurationProperty(),
            ),
        )

        # --- KB_CONFIG Data Source ---
        self._kb_config_ds = bedrock.CfnDataSource(
            self,
            "KBConfigDataSource",
            knowledge_base_id=self._kb_config.attr_knowledge_base_id,
            name="kb-config-s3-source",
            description="S3 data source for enriched configurations (kb-config/ prefix)",
            data_source_configuration=bedrock.CfnDataSource.DataSourceConfigurationProperty(
                type="S3",
                s3_configuration=bedrock.CfnDataSource.S3DataSourceConfigurationProperty(
                    bucket_arn=kb_config_bucket.bucket_arn,
                    inclusion_prefixes=["kb-config/"],
                ),
            ),
        )

        # --- KB_LOGS Knowledge Base ---
        self._kb_logs = bedrock.CfnKnowledgeBase(
            self,
            "KBLogs",
            name="KB_LOGS",
            description=(
                "Base de conhecimento de logs normalizados e histórico de "
                "incidentes de MediaLive, MediaPackage, MediaTailor e CloudFront."
            ),
            role_arn=kb_logs_role.role_arn,
            knowledge_base_configuration=bedrock.CfnKnowledgeBase.KnowledgeBaseConfigurationProperty(
                type="VECTOR",
                vector_knowledge_base_configuration=bedrock.CfnKnowledgeBase.VectorKnowledgeBaseConfigurationProperty(
                    embedding_model_arn=embedding_model_arn,
                ),
            ),
            storage_configuration=bedrock.CfnKnowledgeBase.StorageConfigurationProperty(
                type="S3_VECTORS",
                s3_vectors_configuration=bedrock.CfnKnowledgeBase.S3VectorsConfigurationProperty(),
            ),
        )

        # --- KB_LOGS Data Source ---
        self._kb_logs_ds = bedrock.CfnDataSource(
            self,
            "KBLogsDataSource",
            knowledge_base_id=self._kb_logs.attr_knowledge_base_id,
            name="kb-logs-s3-source",
            description="S3 data source for structured log events (kb-logs/ prefix)",
            data_source_configuration=bedrock.CfnDataSource.DataSourceConfigurationProperty(
                type="S3",
                s3_configuration=bedrock.CfnDataSource.S3DataSourceConfigurationProperty(
                    bucket_arn=kb_logs_bucket.bucket_arn,
                    inclusion_prefixes=["kb-logs/"],
                ),
            ),
        )

    @property
    def kb_config_id(self) -> str:
        """Return the Knowledge Base ID for KB_CONFIG."""
        return self._kb_config.attr_knowledge_base_id

    @property
    def kb_logs_id(self) -> str:
        """Return the Knowledge Base ID for KB_LOGS."""
        return self._kb_logs.attr_knowledge_base_id
