"""Use the OS trust store on Windows while keeping certificate/hostname checks."""
import ssl
import sys


def tls_context():
    if sys.platform == "win32":
        try:
            import truststore
        except ImportError as error:
            raise RuntimeError("Install project dependencies (pip install -e .) for Windows TLS truststore") from error
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    return ssl.create_default_context()
