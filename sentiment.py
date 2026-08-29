import requests
from bs4 import BeautifulSoup
from transformers import pipeline

sentiment_pipeline = pipeline("sentiment-analysis")

def get_yahoo_sentiment(symbol):
    url = f"https://finance.yahoo.com/quote/{symbol}/news"
    page = requests.get(url)
    soup = BeautifulSoup(page.text, "html.parser")

    headlines = [h.get_text() for h in soup.select("h3")]
    scores = []
    for h in headlines[:5]:
        result = sentiment_pipeline(h)[0]
        if result["label"] == "POSITIVE":
            scores.append(result["score"])
        elif result["label"] == "NEGATIVE":
            scores.append(-result["score"])
    return sum(scores) / len(scores) if scores else 0.0
