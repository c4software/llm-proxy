"""
L'EXÉCUTEUR de code : un service à part du proxy, dans le même compose,
NON EXPOSÉ. Lui seul a podman ; le proxy lui parle en HTTP (outil hébergé
`code_execution`, proxy/llm_proxy/tools/code_execution.py). Il ne partage
avec le proxy ni secret, ni volume, ni réseau vers l'extérieur.

Ce paquet (services/executor/executor/) :
  sandbox.py   les bacs à sable (podman sans root) ;
  server.py    la petite API HTTP devant eux ;
  validate.py  la validation sur un vrai moteur (python -m executor.validate).
Un cran plus haut (services/executor/), ce qui fait l'image :
  Dockerfile   l'image du service, qui EMBARQUE le système de fichiers
               des bacs (sandbox/) ;
  sandbox/     ce que contient un bac : bibliothèques Python, contrôle ;
  tests/       les tests du service, sur un faux podman.
"""
