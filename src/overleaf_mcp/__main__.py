"""Enable ``python -m overleaf_mcp`` as a fallback launcher.

If the user has the package installed into a plain Python environment
but no ``overleaf-mcp`` console script on PATH (e.g. because the package
was installed into a venv that is not activated, or because pip was run
without scripts), this module lets them still start the server::

    python -m overleaf_mcp

which is exactly equivalent to running the ``overleaf-mcp`` entry point.
"""

from .server import main

if __name__ == "__main__":
    main()
