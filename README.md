# VendorWatch — Agency 2026 Hackathon

**Défi 5 · Concentration des Fournisseurs · 29 avril 2026**

> L'agent autonome qui voit ce dont le gouvernement ne peut plus se passer.

---

## Setup rapide

```bash
# 1. Cloner
git clone https://github.com/VOTRE_REPO/vendorwatch.git
cd vendorwatch

# 2. Environnement Python
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 3. Dépendances
pip install -r requirements.txt

# 4. Variables d'environnement
cp .env .env.local
# Remplir AWS_ACCESS_KEY_ID et AWS_SECRET_ACCESS_KEY avec les credentials Workshop Studio

# 5. Lancer l'interface
streamlit run app.py
```

## Architecture

5 agents AWS Bedrock — Orchestrateur (Opus), SQL (Sonnet), Analyste (Opus), Narrateur (Sonnet), Watchdog (Haiku).

## Branches Git

- `feat/data-pipeline` → Personne A — db/, memory/, pipeline/
- `feat/agents-core`   → Personne B — agents/sql_agent.py + analyst_agent.py
- `feat/agents-comm`   → Personne C — agents/narrator_agent.py + watchdog_agent.py + orchestrator.py
- `feat/interface`     → Interface Streamlit — app.py, ui/

## Findings clés

- IBM Alberta : 194.6 M$ / 8 ans / 93.6% sole-source
- Réseau bi-gouvernemental : 11 orgs / 4.77 G$ / FED × AB
- CAMH + Foundation : 9 admins communs / 452 M$
- Collège Boréal + Fondation : 29 admins communs / 383.7 M$
