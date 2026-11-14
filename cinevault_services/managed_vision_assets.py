"""Release-pinned upstream assets for the bundled vision preset."""
from dataclasses import dataclass


MODEL_ID = 'Qwen3-VL-2B-Instruct'
MODEL_REVISION = '52d6c8ffea26cc873ac5ad116f8631268d7eb503'
MODEL_REPOSITORY = 'Qwen/Qwen3-VL-2B-Instruct-GGUF'
RUNTIME_RELEASE = 'b12284'


@dataclass(frozen=True)
class Asset:
    name: str
    url: str
    size: int
    sha256: str


def _model_asset(name, size, sha256):
    return Asset(name, f'https://huggingface.co/{MODEL_REPOSITORY}/resolve/{MODEL_REVISION}/{name}', size, sha256)


def _runtime_asset(name, asset_id, size, sha256):
    return Asset(name, f'https://api.github.com/repos/ggml-org/llama.cpp/releases/assets/{asset_id}', size, sha256)


WEIGHTS = _model_asset('Qwen3VL-2B-Instruct-Q4_K_M.gguf', 1107409952,
                       '089d75c52f4b7ffc56ba998ffc50aae89fcafc755f9e7208aacca281dca6c2ae')
PROJECTOR = _model_asset('mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf', 445053216,
                         'f9a68fabba69c3b81e153367b2c7521030b0fa8bb0de400c9599c8e6725f9c82')
RUNTIMES = {
    'windows-x64': _runtime_asset('llama-b12284-bin-win-cpu-x64.zip', 735593662, 25058030,
                                  '6b3d2c7034b0c0bac65aa0524ddf01ba9c2b622ce7e32675976df642627defa9'),
    'macos-arm64': _runtime_asset('llama-b12284-bin-macos-arm64.tar.gz', 735591004, 16870569,
                                  '7965e7701df311c49cfc629cfe14cc5092fcc590ef8c85f4c436a2973031e9d0'),
    'macos-x64': _runtime_asset('llama-b12284-bin-macos-x64.tar.gz', 735591056, 12613082,
                                '4513149cdc86dff07dcfafcaca1b3522b81a2ccd6476e32b9ef185b3fcdbbe49'),
}
