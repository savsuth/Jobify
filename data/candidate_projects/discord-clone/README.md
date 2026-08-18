# Discord Application

![Next.js](https://img.shields.io/badge/Next.js-16-black?logo=next.js&logoColor=white)
![TypeScript](https://img.shields.io/badge/TypeScript-5-blue?logo=typescript&logoColor=white)
![Prisma](https://img.shields.io/badge/Prisma-ORM-2D3748?logo=prisma&logoColor=white)
![License](https://img.shields.io/github/license/savsuth/discord-app)

A Discord-style communication platform built with Next.js, Prisma, and LiveKit. Supports servers, channels, text and audio/video chat, file uploads, invites, member roles, and realtime presence.

---

## Features

- Authentication via Clerk, with automatic user profile bootstrapping
- Servers, text/audio/video channels, direct messages, and 1:1 video calls
- Realtime messaging through Socket.io, with a polling fallback indicator
- File uploads via UploadThing, with emoji picker support
- LiveKit-powered audio/video rooms with in-room chat
- Invite links and member management (roles, kick)
- Infinite message loading using `@tanstack/react-query`
- Light and dark theme support, responsive UI built with Tailwind CSS and Radix primitives

## Tech Stack

| Category | Technologies |
|---|---|
| Framework | Next.js 16 (App Router), React 19, TypeScript |
| Database | PostgreSQL (Supabase), Prisma ORM via `pg` adapter |
| Realtime | Socket.io, LiveKit |
| Auth | Clerk |
| Uploads | UploadThing |
| UI | Tailwind CSS v4, shadcn/Radix UI |

## Prerequisites

- Node.js 18.17 or higher (20.x recommended)
- A PostgreSQL or Supabase dat