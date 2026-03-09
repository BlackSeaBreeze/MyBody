"""
MyBody — бэкенд: ежедневные рекомендации и чат-агент на Gemini.
Данные из приложения (фото еды, витамины, активность) — один контекст для отчёта и чата.
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(
    title="MyBody API",
    description="Здоровье, питание, сон — рекомендации и чат с агентом",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return {"service": "MyBody", "status": "ok"}


@app.get("/health")
def health():
    return {"status": "healthy"}
