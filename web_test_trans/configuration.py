"""Safe YAML parameters, shared by exported web/native replay projects."""
import os
import re
from pathlib import Path

import yaml


def load_config(path):
    def expand(value):
        if isinstance(value, dict):
            return {key:expand(item) for key,item in value.items()}
        if isinstance(value, list):
            return [expand(item) for item in value]
        if isinstance(value, str):
            def variable(match):
                name=match.group(1)
                if name not in os.environ:
                    raise ValueError(f'请设置环境变量 {name}，或在本地 config.yml 中提供参数')
                return os.environ[name]
            return re.sub(r'\$\{([A-Z_][A-Z0-9_]*)\}',variable,value)
        return value
    data=yaml.safe_load(Path(path).read_text('utf-8'))
    if not isinstance(data,dict):
        raise ValueError('config.yml 必须包含参数映射')
    return expand(data)
