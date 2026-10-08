import re

SKILLS = """python java javascript typescript c# c++ golang rust ruby php swift kotlin scala sql nosql bash powershell
react angular vue svelte next.js node.js express django flask fastapi spring spring boot .net asp.net rails laravel graphql restful grpc
html css dynamics sass tailwind redux jquery webpack vite
aws azure gcp google cloud kubernetes docker terraform ansible helm jenkins gitlab_ci github actions ci/cd cloudformation pulumi
linux unix nginx apache istio prometheus grafana datadog splunk elk
postgresql mysql mongodb redis elasticsearch dynamodb cassandra oracle sql server snowflake bigquery redshift databricks
spark hadoop kafka airflow dbt etl pandas numpy scikit-learn tensorflow pytorch machine learning deep learning nlp llm
power bi tableau looker excel salesforce sap servicenow workday
agile scrum jira confluence devops sre microservices serverless iam vpc
selenium playwright cypress jest pytest junit
git ios android react native flutter figma
""".split("\n")


def _build() -> list[str]:
    toks: list[str] = []
    multi = [
        "spring boot",
        "google cloud",
        "github actions",
        "sql server",
        "power bi",
        "machine learning",
        "deep learning",
        "react native",
    ]
    text = " ".join(SKILLS)
    for m in multi:
        text = text.replace(m, m.replace(" ", "_"))
    for t in text.split():
        toks.append(t.replace("_", " "))
    return sorted(set(toks), key=len, reverse=True)


VOCAB = _build()
_PATTERNS = {
    s: re.compile(r"(?<![A-Za-z0-9+#.])" + re.escape(s) + r"(?![A-Za-z0-9+#])", re.I)
    for s in VOCAB
}


def find_skills(text: str) -> list[str]:
    """Extract curated technology skills from arbitrary text (resume or JD)."""
    if not text:
        return []
    found = [s for s, p in _PATTERNS.items() if p.search(text)]
    return sorted(found)
