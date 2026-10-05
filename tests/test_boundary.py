import ast
import pathlib
import tomllib

import pytest

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
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    deps = project["dependencies"] + [d for extra in project.get("optional-dependencies", {}).values() for d in extra]
    assert not [d for d in deps if d.lower().startswith(FORBIDDEN_DEPS)]


def test_no_env_switches_or_env_files():
    text = "\n".join(p.read_text(encoding="utf-8") for p in SRC.rglob("*.py"))
    for word in ("load_dotenv", '_READ_ONLY"', '_WRITES"', "ACCESS_PROFILE", "ROUTER_MODE", ".mist-lab.env"):
        assert word not in text, word


ENV_NAMES = {"environ", "environb", "getenv", "getenvb", "putenv", "unsetenv", "expandvars"}
# Every environment read in the package, by file. The login module reads the logins; the only
# other read is where Windows keeps per-user app data, in the cache folder helper.
ENV_READS = {
    "core/logins.py": ["read_logins: os.environ"],
    "specs_index.py": ["cache_dir: os.environ.get('LOCALAPPDATA')"],
}


def _env_reads(source):
    """Each environment read as "<function>: <code>"; an ``environ.get("NAME")`` call shows whole."""
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    found = []
    for node in ast.walk(tree):
        # Attributes (os.environ) and imports (from os import getenv); a bare name such as an
        # ``environ`` parameter is a plain mapping handed in, not a read.
        if isinstance(node, ast.alias) and node.name in ENV_NAMES:
            code = node.name
        elif isinstance(node, ast.Attribute) and node.attr in ENV_NAMES:
            get = parents.get(node)
            call = parents.get(get)
            whole = (
                isinstance(get, ast.Attribute) and get.attr == "get" and isinstance(call, ast.Call) and call.func is get
            )
            code = ast.unparse(call if whole else node)
        else:
            continue
        names, up = [], parents.get(node)
        while up is not None:
            if isinstance(up, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.append(up.name)
            up = parents.get(up)
        found.append(f"{'.'.join(reversed(names)) or '<module>'}: {code}")
    return found


@pytest.mark.parametrize(
    ("source", "reads"),
    [
        ("def cache_dir():\n    return os.environ.get('LOCALAPPDATA')", ["cache_dir: os.environ.get('LOCALAPPDATA')"]),
        ("def cache_dir():\n    return os.environ.get('HOME')", ["cache_dir: os.environ.get('HOME')"]),
        ("def cache_dir():\n    return os.environ['LOCALAPPDATA']", ["cache_dir: os.environ"]),
        ("def other():\n    return os.environ.get('LOCALAPPDATA')", ["other: os.environ.get('LOCALAPPDATA')"]),
        ("def cache_dir():\n    return os.getenv('LOCALAPPDATA')", ["cache_dir: os.getenv"]),
        ("from os import environ", ["<module>: environ"]),
        ("def cache_dir():\n    return Path.home()", []),
    ],
)
def test_env_read_finder(source, reads):
    assert _env_reads(source) == reads


def test_environment_reads_are_only_the_known_ones():
    found = {}
    for path in SRC.rglob("*.py"):
        reads = _env_reads(path.read_text(encoding="utf-8"))
        if reads:
            found[path.relative_to(SRC).as_posix()] = reads
    assert found == ENV_READS


def test_no_ingestion_folder():
    assert not (ROOT / "ingestion").exists() and not (ROOT / "data").exists()
