"""Reddit subreddit mention tracking - intentionally not implemented.

Since November 2025 Reddit requires manual pre-approval for every new Data API
user (Responsible Builder Policy), including personal, non-commercial scripts.
Per this project's rules, sources that need approval are skipped rather than
worked around. If access is ever approved, a reader here can store mentions
via Storage.add_mentions() exactly like sources/telegram.py, and the social
score will pick them up automatically.
"""
