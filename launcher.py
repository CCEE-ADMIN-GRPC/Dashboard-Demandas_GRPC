"""Inicia o dashboard local e abre sua interface no navegador padrão."""

import os
import socket
import sys
import threading
import webbrowser
from pathlib import Path


def choose_port(start=8050, end=8100):
    for port in range(start, end):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
            try:
                server_socket.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("Não há uma porta local disponível entre 8050 e 8099.")


def main():
    app_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    os.chdir(app_dir)

    from app import app

    port = choose_port()
    address = f"http://127.0.0.1:{port}"
    threading.Timer(1.2, webbrowser.open, args=(address,)).start()
    print(f"Dashboard Regras iniciado em {address}")
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
