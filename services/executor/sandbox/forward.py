"""
Dans le BAC : le relais de sa sortie vers les dépôts de paquets.

Le bac n'a pas de réseau (`--network none`) ; seule sa boucle locale
existe. Quand l'exécuteur lui ouvre une sortie (SANDBOX_EGRESS), il monte
une prise Unix en /run/egress.sock et lance ce script au démarrage du bac.
Il écoute sur 127.0.0.1:3128 — l'adresse de HTTPS_PROXY dans le bac — et
passe chaque connexion, octet pour octet, à la prise. Il ne juge RIEN :
c'est l'exécuteur, de l'autre côté de la prise, qui n'accepte que
`CONNECT` vers les dépôts autorisés. Le programme du modèle peut tuer ce
relais (même uid) : il ne perd alors que sa propre sortie.
"""

import asyncio

SOCKET = "/run/egress.sock"
LISTEN = ("127.0.0.1", 3128)


async def pipe(src, dst):
    try:
        while chunk := await src.read(65536):
            dst.write(chunk)
            await dst.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        dst.close()


async def handle(reader, writer):
    try:
        ureader, uwriter = await asyncio.open_unix_connection(SOCKET)
    except OSError:
        writer.close()
        return
    await asyncio.gather(pipe(reader, uwriter), pipe(ureader, writer))


async def main():
    server = await asyncio.start_server(handle, *LISTEN)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
