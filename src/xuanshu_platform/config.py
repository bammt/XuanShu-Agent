from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://xuanshu:change-me@postgres:5432/xuanshu"
    redis_url: str = "redis://redis:6379/0"
    minio_endpoint: str = "minio:9000"
    minio_access_key: str = "xuanshu"
    minio_secret_key: str = "change-me"
    minio_bucket: str = "xuanshu-files"
    minio_secure: bool = False
    qdrant_url: str = "http://qdrant:6333"
    jwt_secret: str = "change-this-in-production"
    encryption_key: str = "replace-with-a-fernet-key"
    admin_username: str = "admin"
    admin_password: str = "change-me-now"
    max_upload_mb: int = 50
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    executor_url: str = "http://executor:8020"
    executor_shared_secret: str = "replace-executor-secret"
    executor_max_snapshot_mb: int = 50
    code_timeout_seconds: int = 60
    run_max_retries: int = 3
    run_retry_base_seconds: float = 1.5
    conversation_lock_seconds: int = 180
    conversation_history_token_budget: int = 6000
    conversation_summary_max_chars: int = 4000
    conversation_retention_days: int = 30
    external_session_retention_days: int = 30
    external_upload_retention_days: int = 30
    access_token_expire_minutes: int = 720
    crewai_platform_integration_token: str = ""
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()


def validate_production_settings() -> None:
    weak = {
        'POSTGRES_PASSWORD': settings.database_url if 'change-me@' in settings.database_url else '',
        'MINIO_SECRET_KEY': settings.minio_secret_key if settings.minio_secret_key == 'change-me' else '',
        'JWT_SECRET': settings.jwt_secret if settings.jwt_secret == 'change-this-in-production' else '',
        'ENCRYPTION_KEY': settings.encryption_key if settings.encryption_key == 'replace-with-a-fernet-key' else '',
        'ADMIN_PASSWORD': settings.admin_password if settings.admin_password == 'change-me-now' else '',
        'EXECUTOR_SHARED_SECRET': settings.executor_shared_secret if settings.executor_shared_secret == 'replace-executor-secret' else '',
    }
    invalid = [name for name, value in weak.items() if value]
    if len(settings.jwt_secret) < 32:
        invalid.append('JWT_SECRET')
    if len(settings.admin_password) < 12:
        invalid.append('ADMIN_PASSWORD')
    if len(settings.executor_shared_secret) < 24:
        invalid.append('EXECUTOR_SHARED_SECRET')
    if invalid:
        raise RuntimeError(f"生产密钥未配置或强度不足：{', '.join(dict.fromkeys(invalid))}")
