import subprocess
import sys

from vector_lake.tokenizer_runtime import segment_text, tokenize_for_fts


def test_tokenizer_passes_strict_warning_gate_without_legacy_jieba():
    code = (
        "import sys; "
        "from vector_lake.tokenizer_runtime import tokenize_for_fts; "
        "assert tokenize_for_fts('三级医院评审标准'); "
        "assert 'jieba' not in sys.modules; "
        "assert 'pkg_resources' not in sys.modules"
    )
    result = subprocess.run(
        [sys.executable, "-W", "error", "-c", code],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_tokenizer_keeps_index_and_query_representation_identical():
    text = "三级医院评审标准"
    assert segment_text(text) == ["三级", "医院", "评审", "标准"]
    assert tokenize_for_fts(text) == " ".join(segment_text(text))
