# Marvis Bot

> **A must-have Personal AI for people who keep forgetting ideas or find scheduling annoying.**  
> **My + Jarvis = Marvis**

Marvis Bot is a personal Telegram-based AI assistant running on an Oracle Cloud Ubuntu server.

---

## 🇺🇸 English

## Introduction

**Marvis Bot** is a personal Telegram-based AI assistant running on an Oracle Cloud Ubuntu server.

When the user sends a message through Telegram, Marvis generates a response using the Gemini API, converts the answer into a Korean mp3 voice file using gTTS, and sends both the text and audio response back through Telegram.

> A must-have Personal AI for people who keep forgetting ideas or find scheduling annoying.  
> **My + Jarvis = Marvis**

## Project Goal

The goal of this project is to build a lightweight personal AI assistant that can be used quickly from Telegram.

Main workflow:

```txt
Send a Telegram message
→ Generate an answer with Gemini API
→ Convert the answer into Korean speech with gTTS
→ Send both text and mp3 audio back to Telegram
→ Listen with AirPods while moving