# Session rules

- Valid event types are page_view, login, and purchase.
- More than 30 minutes of inactivity starts a new session. Exactly 30 minutes remains in the same session.
- A purchase event marks the session converted.
- Bot users are excluded entirely.
- Anonymous events are stitched to a user if a later login event on the same anonymous_id supplies user_id.
