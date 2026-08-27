from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    port: int = 8000
    database_url: str = "sqlite:///recovery.db"
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    gemini_api_key: str = ""
    mandate_secret: str = "dev-secret-change-me-32chars!!"
    gemini_enabled: bool = False
    wa_phone_id: str = ""
    wa_token: str = ""
    wa_to: str = "+919560452773"
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""
    sarvam_api_key: str = ""


settings = Settings()
