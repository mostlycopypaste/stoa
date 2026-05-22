-- Migration 008: Add updated_at column to posts table

ALTER TABLE posts ADD COLUMN updated_at DATETIME;
