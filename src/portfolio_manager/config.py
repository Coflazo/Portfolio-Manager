from pathlib import Path

import yaml

from .models import Settings


def load_settings(path: str | Path = "config.yaml") -> Settings:
    with Path(path).open() as stream:
        return Settings.model_validate(yaml.safe_load(stream))
