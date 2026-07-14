from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LogFileConfig(ConfigModel):
    enable: bool
    level: str
    path: Path
    rotation: str
    retention: str


class LogConsoleConfig(ConfigModel):
    enable: bool
    level: str


class LoggingConfig(ConfigModel):
    file: LogFileConfig
    console: LogConsoleConfig


class AppConfig(ConfigModel):
    logging: LoggingConfig

    @classmethod
    def from_yaml(
        cls, path: str | Path = Path(__file__).parents[2] / "conf" / "app.yaml"
    ) -> "AppConfig":
        with Path(path).open(encoding="utf-8") as file:
            return cls.model_validate(yaml.safe_load(file))


if __name__ == "__main__":
    config = AppConfig.from_yaml()
    assert config.logging.file.path == Path("logs")
