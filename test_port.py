import socket

def check_port(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1)
    try:
        s.connect(("127.0.0.1", port))
        print(f"Port {port} is OPEN")
        s.close()
        return True
    except:
        print(f"Port {port} is CLOSED")
        return False

check_port(8001)
