"""Test that the package imports and the core interfaces are sound."""
import re
from pathlib import Path

from agent_loop.profiles import Profile, register, get, DEFAULT_PROTECTED
from agent_loop.models import ModelConfig, ModelRegistry, DEFAULT_REGISTRY
from agent_loop.gates import GateResult, check_protected_paths, check_static
from agent_loop.regions import RegionError, strip_code, strip_code_default
from agent_loop.providers import ProviderError, chat, Completion, split_model


def test_import():
    """Package imports, and __version__ is the single source of truth.

    Asserting a literal here means every release breaks this test for no
    reason. The invariant that matters is that __version__ and the version
    setuptools ships from pyproject.toml cannot drift apart -- a mismatch
    makes an installed wheel report a version it is not.
    """
    import agent_loop
    assert re.fullmatch(r"\d+\.\d+\.\d+", agent_loop.__version__), agent_loop.__version__

    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    declared = re.search(r'^version\s*=\s*"([^"]+)"', pyproject.read_text(), re.M)
    assert declared, "pyproject.toml has no version"
    assert agent_loop.__version__ == declared.group(1), (
        f"__init__.py says {agent_loop.__version__}, pyproject.toml says {declared.group(1)}"
    )


def test_profile_dataclass():
    """Profile has all the language-agnostic fields from the plan."""
    p = Profile(
        name="test",
        language="python",
        file_suffixes=(".py",),
        line_comment="#",
        block_comment=(),  # Python has no block comments
        implementer_rules="test rules",
        reviewer_priorities="test priorities",
    )
    assert p.context_token_budget == 3000
    assert p.round_input_token_budget == 40000
    assert p.lock_name == ""
    assert p.file_scope_whitelist == ()
    assert "test rules" in p.implementer_system
    assert "test priorities" in p.reviewer_system


def test_profile_register_and_get():
    """Profiles can be registered and retrieved."""
    p = Profile(name="test-register", language="python", file_suffixes=(".py",))
    register(p)
    assert get("test-register") is p


def test_model_registry_validate():
    """Registry rejects an arbiter that matches a reviewer."""
    reg = ModelRegistry()
    reg.register(ModelConfig("model-a", "reviewer", "fast", 0.0))
    reg.register(ModelConfig("model-a", "arbiter", "strong-reasoner", 0.0))
    try:
        reg.validate("model-a", ["model-a"], "model-a")
        assert False, "should have raised"
    except ValueError:
        pass


def test_model_registry_validate_ok():
    """Registry accepts an arbiter from a different family."""
    reg = ModelRegistry()
    reg.register(ModelConfig("model-a", "reviewer", "fast", 0.0))
    reg.register(ModelConfig("model-b", "arbiter", "strong-reasoner", 0.0))
    reg.validate("model-c", ["model-a"], "model-b")


def test_split_model():
    """Model spec parsing works for all backends."""
    assert split_model("ollama:kimi:cloud") == ("ollama", "kimi:cloud")
    assert split_model("anthropic:claude-opus-5") == ("anthropic", "claude-opus-5")
    assert split_model("openai:gpt-4o") == ("openai", "gpt-4o")
    assert split_model("bare-model-name") == ("ollama", "bare-model-name")


def test_strip_code_python():
    """strip_code handles Python line comments."""
    p = Profile(name="py", language="python", file_suffixes=(".py",), line_comment="#", block_comment=())
    assert strip_code("x = 1  # comment", p) == "x = 1  "
    assert strip_code('y = "hello # world"', p) == 'y = '


def test_strip_code_csharp():
    """strip_code handles C# line comments."""
    p = Profile(name="cs", language="csharp", file_suffixes=(".cs",), line_comment="//", block_comment=("/*", "*/"))
    assert strip_code("int x = 1; // comment", p) == "int x = 1; "
    assert strip_code('string s = "hello // world";', p) == "string s = ;"


def test_check_protected_paths_ok():
    """Protected paths gate passes clean files."""
    result = check_protected_paths(["src/main.py", "src/utils.py"])
    assert result.ok
    assert "2 region file(s) clear" in result.summary


def test_check_protected_paths_blocks_tests():
    """Protected paths gate blocks test files."""
    result = check_protected_paths(["src/main.py", "tests/test_main.py"])
    assert not result.ok
    assert "test_main.py" in result.detail