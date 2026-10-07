"""
L'EXÉCUTEUR de code : un service à part du proxy, dans le même compose,
NON EXPOSÉ. Lui seul a podman ; le proxy lui parle en HTTP (outil hébergé
`code_execution`, llm_proxy/tools/code_execution.py). Il ne partage avec
le proxy ni secret, ni volume, ni réseau vers l'extérieur.

  sandbox.py   les bacs à sable (podman sans root) ;
  server.py    la petite API HTTP devant eux ;
  Dockerfile   l'image du service, qui EMBARQUE le système de fichiers
               des bacs (sandbox/) ;
  sandbox/     ce que contient un bac : bibliothèques Python, contrôle.
"""
