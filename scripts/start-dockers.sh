#!/bin/bash
docker start mon-nginx mon-postgres mon-json-api
echo "Conteneurs démarrés :"
docker ps

cd ~/assistant-ia && source venv/bin/activate
