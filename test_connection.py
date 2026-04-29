"""
Script de test à lancer avant le 29 avril.
Vérifie que tout est en ordre.
"""
import sys

print("=== VendorWatch — Tests pré-hackathon ===\n")

# Test 1 — Imports Python
print("1. Test imports Python...")
try:
    import psycopg2
    import pandas as pd
    import numpy as np
    import boto3
    import anthropic
    import streamlit
    print("   ✅ Tous les imports OK")
except ImportError as e:
    print(f"   ❌ Import manquant : {e}")
    print("   → pip install -r requirements.txt")

# Test 2 — Variables d'environnement
print("\n2. Test variables d'environnement...")
from dotenv import load_dotenv
import os
load_dotenv()
db_url = os.getenv("DB_CONNECTION_STRING")
aws_key = os.getenv("AWS_ACCESS_KEY_ID")
anthropic_key = os.getenv("ANTHROPIC_API_KEY")

if db_url:
    print("   ✅ DB_CONNECTION_STRING présent")
else:
    print("   ❌ DB_CONNECTION_STRING manquant dans .env")

if aws_key:
    print("   ✅ AWS_ACCESS_KEY_ID présent")
else:
    print("   ⚠️  AWS_ACCESS_KEY_ID vide — remplir le 29 matin avec Workshop Studio")

if anthropic_key:
    print("   ✅ ANTHROPIC_API_KEY présent (backup LLM)")
else:
    print("   ⚠️  ANTHROPIC_API_KEY vide — backup LLM non disponible")

# Test 3 — Connexion PostgreSQL
print("\n3. Test connexion PostgreSQL Render...")
if db_url:
    try:
        import psycopg2
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM fed.vw_grants_decoded WHERE agreement_value > 0")
        count_fed = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM ab.ab_sole_source WHERE amount > 0")
        count_ab = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM general.entity_golden_records")
        count_gen = cur.fetchone()[0]
        conn.close()
        print(f"   ✅ FED : {count_fed:,} lignes")
        print(f"   ✅ AB sole-source : {count_ab:,} lignes")
        print(f"   ✅ Golden records : {count_gen:,} lignes")
    except Exception as e:
        print(f"   ❌ Connexion échouée : {e}")
else:
    print("   ⏭️  Ignoré (DB_CONNECTION_STRING manquant)")

# Test 4 — PDF export
print("\n4. Test export PDF...")
try:
    import weasyprint
    weasyprint.HTML(string="<h1>VendorWatch Test</h1>").write_pdf("/tmp/test_vw.pdf")
    print("   ✅ weasyprint OK")
except Exception as e:
    print(f"   ⚠️  weasyprint échoué : {e}")
    try:
        from reportlab.pdfgen import canvas
        c = canvas.Canvas("/tmp/test_vw_rl.pdf")
        c.drawString(100, 750, "VendorWatch Test")
        c.save()
        print("   ✅ reportlab OK (backup)")
    except Exception as e2:
        print(f"   ❌ reportlab aussi échoué : {e2}")
        print("   → Repli sur HTML print() disponible")

# Test 5 — Strands
print("\n5. Test Strands Agents SDK...")
try:
    from strands import Agent
    from strands.models import BedrockModel
    print("   ✅ strands-agents importable")
except ImportError as e:
    print(f"   ❌ strands-agents manquant : {e}")
    print("   → pip install strands-agents strands-agents-tools")

print("\n=== Tests terminés ===")
print("Si tout est ✅ ou ⚠️, tu es prêt pour demain.")
print("Les ❌ doivent être résolus ce soir.")
