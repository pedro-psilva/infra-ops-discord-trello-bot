#!/usr/bin/env python3
import py_compile
import sys

try:
    py_compile.compile('tests/test_service.py', doraise=True)
    print("Sintaxe OK - Arquivo compilado com sucesso!")
    sys.exit(0)
except py_compile.PyCompileError as e:
    print(f"Erro de sintaxe: {e}")
    sys.exit(1)
