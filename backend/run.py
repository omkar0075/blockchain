import sys
from pathlib import Path
import uvicorn

if __name__ == "__main__":
    # Ensure backend directory is in python path
    backend_dir = Path(__file__).resolve().parent
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))

    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
        reload_dirs=["app"],
        reload_includes=["*.py"],
        reload_excludes=[".venv/*", "test_*.py", "*.db", ".pytest_cache/*"],
    )

