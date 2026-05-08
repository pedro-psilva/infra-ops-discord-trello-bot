#!/usr/bin/env python3
"""Verifica se os testes podem ser importados e compilados."""
import sys
import ast

# Verifica se o arquivo Python tem sintaxe valida
filepath = 'tests/test_service.py'
try:
    with open(filepath, 'r', encoding='utf-8') as f:
        source_code = f.read()
    ast.parse(source_code)
    print(f"✓ Arquivo {filepath} tem sintaxe valida")

    # Conta as classes de teste
    tree = ast.parse(source_code)
    test_classes = [node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef) and 'Test' in node.name]
    print(f"✓ Classes de teste encontradas: {len(test_classes)}")
    for cls in test_classes:
        print(f"  - {cls}")

except SyntaxError as e:
    print(f"✗ Erro de sintaxe: {e}")
    sys.exit(1)
except Exception as e:
    print(f"✗ Erro ao verificar: {e}")
    sys.exit(1)
