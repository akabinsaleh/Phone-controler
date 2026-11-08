import socket, threading
def pump(a, b):
    try:
        while True:
            d = a.recv(65536)
            if not d: break
            b.sendall(d)
    except OSError: pass
    finally:
        for s in (a, b):
            try: s.shutdown(socket.SHUT_RDWR)
            except OSError: pass
            s.close()
srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); srv.bind(('172.22.208.1', 27016)); srv.listen(16)
while True:
    c, _ = srv.accept()
    try:
        u = socket.create_connection(('127.0.0.1', 27015))
    except OSError:
        c.close(); continue
    threading.Thread(target=pump, args=(c, u), daemon=True).start()
    threading.Thread(target=pump, args=(u, c), daemon=True).start()