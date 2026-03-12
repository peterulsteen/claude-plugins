# Bingo Skill - Context-Triggered SMS Notification

## Problem Statement

Operators monitoring orchestration runs need real-time alerts when specific keywords appear in the running context. Currently, there is no mechanism to trigger external notifications based on context content during an active orchestration session.

## Overview

Implement a "bingo" skill for the Symphony orchestration system that monitors the orchestration's running context and sends an SMS message to the configured operator whenever the word "bingo" appears. This requires skill registration, context monitoring, SMS integration via Twilio, and configuration management.

## User Stories

- As an operator, I want to receive an SMS when "bingo" appears in the context so that I am immediately notified of important events.
- As a developer, I want to configure the SMS recipient and trigger word so that the skill is reusable across deployments.
- As a system administrator, I want the skill to gracefully handle SMS delivery failures so that the orchestration continues uninterrupted.

## Requirements

### Functional Requirements

- FR-1: Skill definition file (`SKILL.md`) conforming to the Symphony skill format with YAML frontmatter
- FR-2: Context monitoring hook that scans new context entries for the trigger word
- FR-3: SMS message delivery via Twilio API when trigger word is detected
- FR-4: Configuration for: trigger word (default: "bingo"), recipient phone number, Twilio credentials (account SID, auth token, from number)
- FR-5: Deduplication to prevent repeated SMS for the same context entry
- FR-6: Logging of all trigger events and SMS delivery attempts with structured JSON output

### Non-Functional Requirements

- NFR-1: SMS delivery latency < 5 seconds from trigger detection
- NFR-2: Skill must not block or slow the orchestration pipeline (async delivery)
- NFR-3: Graceful degradation if Twilio API is unreachable (log warning, continue)
- NFR-4: Configuration via environment variables with sensible defaults

## Acceptance Criteria

- AC-001: Skill registers and appears in skill listing when installed
- AC-002: SMS is sent within 5 seconds of "bingo" appearing in context
- AC-003: Duplicate triggers within same context entry do not produce duplicate SMS
- AC-004: Configuration can be overridden via environment variables (BINGO_TRIGGER_WORD, BINGO_RECIPIENT, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER)
- AC-005: Orchestration continues normally if SMS delivery fails
- AC-006: All trigger events are logged with timestamps in JSON format
