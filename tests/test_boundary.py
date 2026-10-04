import ast
import pathlib
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "casper_network_mcp"
FORBIDDEN_IMPORTS = {"dotenv", "lancedb", "fastembed", "ollama", "redis", "pymilvus", "playwright", "ingestion"}
FORBIDDEN_DEPS = ("python-dotenv", "lancedb", "fastembed", "ollama", "redis", "pymilvus", "playwright")


def _imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module.split(".")[0]


def test_no_rag_or_scraper_imports():
    bad = {(p.name, m) for p in SRC.rglob("*.py") for m in _imports(p) if m in FORBIDDEN_IMPORTS}
    assert not bad


def test_no_forbidden_dependencies():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    deps = project["dependencies"] + [d for extra in project.get("optional-dependencies", {}).values() for d in extra]
    assert not [d for d in deps if d.lower().startswith(FORBIDDEN_DEPS)]


def test_no_env_switches_or_env_files():
    text = "\n".join(p.read_text(encoding="utf-8") for p in SRC.rglob("*.py"))
    for word in ("load_dotenv", '_READ_ONLY"', '_WRITES"', "ACCESS_PROFILE", "ROUTER_MODE", ".mist-lab.env"):
        assert word not in text, word


def test_no_ingestion_folder():
    assert not (ROOT / "ingestion").exists() and not (ROOT / "data").exists()
