"""Single-attempt HTTPS worker; its parent enforces the wall-clock deadline."""

import http.client
import json
import socket
import sys


def main() -> None:
    request = json.load(sys.stdin)
    connection = http.client.HTTPSConnection(
        "api.heigit.org", timeout=request["connect_timeout"]
    )
    try:
        connection.connect()
        connection.sock.settimeout(request["read_timeout"])
        connection.request(
            "POST", request["path"], json.dumps(request["body"]),
            {"Authorization": request["key"], "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        body = response.read(16 * 1024 * 1024 + 1)
        if len(body) > 16 * 1024 * 1024:
            result = {"error": "invalid_response"}
        else:
            result = {"status": response.status, "body": body.decode("utf-8")}
    except (TimeoutError, socket.timeout):
        result = {"error": "timeout"}
    except (OSError, http.client.HTTPException, UnicodeError):
        result = {"error": "provider_unavailable"}
    finally:
        connection.close()
    json.dump(result, sys.stdout)


if __name__ == "__main__":
    main()
