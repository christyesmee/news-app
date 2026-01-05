import os
import requests
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import google.generativeai as genai
from datetime import datetime

# --- LOAD SECRETS ---
NEWS_API_KEY = os.getenv("NEWS_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL")

# --- CONFIGURATION ---
TOPIC = "Artificial Intelligence"  # Change this to your interest
MODEL_NAME = "gemini-1.5-flash"  # Fast and efficient model

def fetch_news():
    """Get top 5 articles from NewsAPI"""
    url = f"https://newsapi.org/v2/everything?q={TOPIC}&sortBy=publishedAt&language=en&pageSize=5&apiKey={NEWS_API_KEY}"
    try:
        response = requests.get(url)
        data = response.json()
        return data.get("articles", [])
    except Exception as e:
        print(f"Error fetching news: {e}")
        return []

def summarize_news(articles):
    """Send articles to Gemini for summarization"""
    if not articles:
        return "No news found today."

    # Prepare data for AI
    raw_text = "\n\n".join(
        [f"Title: {a['title']}\nSource: {a['source']['name']}\nLink: {a['url']}" for a in articles]
    )

    # Configure Gemini
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(MODEL_NAME)

    prompt = (
        f"Act as a professional news analyst. Summarize these 5 articles about {TOPIC} "
        "into a email briefing. "
        "Format: \n"
        "- Use a catchy headline for the email subject.\n"
        "- For each article, provide a bold title and a 2-sentence summary.\n"
        "- Provide a clickable link (HTML format) to the source.\n"
        "- End with a 'Trend of the Day' insight.\n\n"
        f"Data:\n{raw_text}"
    )

    try:
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        return f"Error generating summary: {e}"

def send_email(content):
    """Send the summary via Gmail"""
    msg = MIMEMultipart()
    msg['From'] = EMAIL_USER
    msg['To'] = RECIPIENT_EMAIL
    msg['Subject'] = f"Daily AI Update: {datetime.now().strftime('%Y-%m-%d')}"

    # We use 'html' here so links work and formatting looks good
    msg.attach(MIMEText(content, 'html')) # Changed to HTML for better formatting

    try:
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.login(EMAIL_USER, EMAIL_PASSWORD)
        server.send_message(msg)
        server.quit()
        print("Email sent successfully!")
    except Exception as e:
        print(f"Failed to send email: {e}")

if __name__ == "__main__":
    print("--- Starting Job ---")
    articles = fetch_news()
    if articles:
        print(f"Found {len(articles)} articles. Summarizing...")
        summary = summarize_news(articles)
        # Convert markdown bolding (**text**) to HTML bolding (<b>text</b>) for email
        html_summary = summary.replace("**", "<b>").replace("**", "</b>").replace("\n", "<br>")
        send_email(html_summary)
    else:
        print("No articles found.")