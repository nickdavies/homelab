#!/bin/bash

op run --env-file=env.template -- ./venv/bin/python create_new_user.py "$@"
