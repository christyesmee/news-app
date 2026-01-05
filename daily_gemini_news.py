import os
import requests
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import google.generativeai as genai
from datetime import datetime, timedelta

# --- CONFIGURATION ---
NEWS_API_KEY = os.getenv("NEWS_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL")

MODEL_NAME = "gemini-1.5-pro"

# 10 Diverse International Sources
SOURCES = [
    "reuters.com", "aljazeera.com", "bbc.co.uk", "scmp.com", 
    "dw.com", "bloomberg.com", "wsj.com", "theguardian.com", 
    "france24.com", "thehindu.com"
]

def fetch_global_news():
    print("--- Fetching World News ---")
    domains = ",".join(SOURCES)
    seven_days_ago = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
    
    # We fetch slightly more articles to ensure we have enough valid ones
    url = (
        f"https://newsapi.org/v2/everything?"
        f"q=(economy OR politics OR international relations) AND (world OR global)&"
        f"domains={domains}&"
        f"from={seven_days_ago}&"
        f"sortBy=popularity&"
        f"language=en&"
        f"pageSize=20&"
        f"apiKey={NEWS_API_KEY}"
    )
    
    try:
        response = requests.get(url)
        data = response.json()
        articles = data.get("articles", [])
        print(f"-> Found {len(articles)} articles.")
        return articles
    except Exception as e:
        print(f"Error fetching news: {e}")
        return []

def analyze_news(articles):
    print(f"--- Analyzing with {MODEL_NAME} ---")
    if not articles:
        return "<p>No significant international news found today.</p>"

    raw_text = "\n\n".join(
        [f"Source: {a['source']['name']}\nTitle: {a['title']}\nSnippet: {a['description']}" for a in articles[:15]]
    )

    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(MODEL_NAME)

    # CRITICAL: Disable Safety Filters so it doesn't block political/war news
    safety_settings = [
        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
    ]

    prompt = (
        "You are an expert Professor of International Relations. "
        "Review these news snippets from diverse global sources for this week. "
        "Create a HTML summary report.\n\n"
        "INSTRUCTIONS:\n"
        "1. Identify the top 3 critical global themes.\n"
        "2. For each theme, create a <div class='section'> block.\n"
        "3. Inside the block:\n"
        "   - Add a headline <div class='news-title'>Theme Title</div>\n"
        "   - Summarize the event in 2-3 sentences. Cite sources using <span class='source-tag'>Source Name</span>.\n"
        "   - CRITICAL: Add a <div class='theory-box'>.\n"
        "     Inside it, define the political/economic THEORY or concept at play here.\n"
        "     (e.g., 'Protectionism', 'Realism', 'Soft Power', 'Inflation').\n"
        "     Label it <span class='theory-title'>CONCEPT: [Name of Theory]</span>.\n\n"
        f"NEWS DATA:\n{raw_text}"
    )

    try:
        # Generate with safety settings
        response = model.generate_content(prompt, safety_settings=safety_settings)
        
        # Check if response was blocked
        if not response.parts:
            print("!! Gemini returned an empty response (likely safety block).")
            return "<p>AI Analysis unavailable due to safety filters on today's news content.</p>"

        clean_html = response.text.replace("```html", "").replace("```", "")
        return clean_html
    except Exception as e:
        print(f"Error during AI analysis: {e}")
        return f"<p>Error during AI analysis: {e}</p>"

def send_email(html_content):
    print("--- Sending Email ---")
    
    # 1. Read Template
    try:
        with open('email_template.html', 'r') as f:
            template_str = f.read()
    except FileNotFoundError:
        print("!! Template file not found. Using basic fallback.")
        template_str = "<html><body><h1>Global Brief</h1><br>{{CONTENT}}</body></html>"

    # 2. Manual Replacement (More robust than Template library)
    # We replace {{DATE}} and {{CONTENT}} directly
    current_date = datetime.now().strftime('%A, %B %d, %Y')
    
    final_body = template_str.replace("{{DATE}}", current_date)
    final_body = final_body.replace("{{CONTENT}}", html_content)

    msg = MIMEMultipart()
    msg['From'] = EMAIL_USER
    msg['To'] = RECIPIENT_EMAIL
    msg['Subject'] = f"🌍 World Brief: {datetime.now().strftime('%Y-%m-%d')}"
    msg.attach(MIMEText(final_body, 'html'))

    try:
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.login(EMAIL_USER, EMAIL_PASSWORD)
        server.send_message(msg)
        server.quit()
        print("vv Email sent successfully!")
    except Exception as e:
        print(f"!! Failed to send email: {e}")

if __name__ == "__main__":
    articles = fetch_global_news()
    if articles:
        analysis_html = analyze_news(articles)
        send_email(analysis_html)
    else:
        print("No articles found.")