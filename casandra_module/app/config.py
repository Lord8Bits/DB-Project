from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    cassandra_hosts: str = "127.0.0.1"
    cassandra_port: int = 9042
    cassandra_keyspace: str = "youtube_nosql"
    comment_shards: int = 4
    like_shards: int = 16
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def hosts(self):
        return [x.strip() for x in self.cassandra_hosts.split(",") if x.strip()]

settings = Settings()
