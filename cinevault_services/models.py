"""Read-only local model configuration; never downloads or imports a model."""
import json
import os
from pathlib import Path
import shutil
import sys


def load_model_config(app,relative,environment,script,model):
    path=Path(os.environ.get(environment,str(app/relative)))
    try:
        config=json.loads(path.read_text('utf-8'))
        if not isinstance(config,dict): return None
        platform='windows' if os.name=='nt' else 'macos' if sys.platform=='darwin' else 'linux'
        def resolve(value):
            value=Path(value)
            return value if value.is_absolute() else app/value
        python=config.get('python_'+platform) or config.get('python')
        if not python: return None
        executable=Path(shutil.which(python)) if Path(python).name==python and shutil.which(python) else resolve(python)
        packages=config.get('packages_'+platform)
        return dict(config,python_path=executable,script_path=resolve(config.get('script',script)),
                    model_path=resolve(config.get('model',model)),packages_path=resolve(packages) if packages else None)
    except (OSError,ValueError,TypeError): return None


def model_available(config):
    return bool(config and all(config[name].is_file() for name in ('python_path','script_path','model_path'))
                and (config.get('packages_path') is None or config['packages_path'].is_dir()))
