#!/bin/bash

source .venv/bin/activate
nohup python -m ggshot_lighter_bot > out.log 2>&1 &