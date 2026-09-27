"""Optional reproducible CPU acceleration of the existing BGE ONNX weights.

Both compared retrieval algorithms must use the same resulting encoder.
"""
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
(ROOT / 'reports/rag').mkdir(parents=True, exist_ok=True)

from onnxruntime.quantization import QuantType, quantize_dynamic
from onnx import TensorProto
from rag.embedding import local_model_path

source = local_model_path()
if not source:
    raise RuntimeError('Download the original FastEmbed BGE model first')
target = ROOT / '.cache/rag/embedding-int8'
if source.resolve() == target.resolve():
    raise ValueError('Select the original FP32 EMBEDDING_LOCAL_PATH before quantization')
target.mkdir(parents=True, exist_ok=True)
for path in source.iterdir():
    if path.is_file() and path.suffix in {'.json', '.model', '.txt'}:
        shutil.copyfile(path, target / path.name)
original = source / 'model_optimized.onnx'
output = target / 'model_optimized.onnx'
quantize_dynamic(str(original), str(output), weight_type=QuantType.QInt8,
                 per_channel=True, op_types_to_quantize=['MatMul', 'Attention'],
                 extra_options={'DefaultTensorType': TensorProto.FLOAT})
def checksum(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()
manifest = {'model': 'BAAI/bge-small-zh-v1.5', 'source': str(source),
            'source_onnx_sha256': checksum(original), 'int8_onnx_sha256': checksum(output),
            'method': 'onnxruntime dynamic QInt8 per-channel MatMul/Attention; no calibration or relevance labels'}
(ROOT / 'reports/rag/embedding-quantization.json').write_text(json.dumps(manifest, indent=2) + '\n')
print(json.dumps(manifest, indent=2))
