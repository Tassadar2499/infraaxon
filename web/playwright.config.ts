import { defineConfig } from '@playwright/test'
export default defineConfig({testDir:'tests',workers:1,use:{baseURL:process.env.CONSOLE_URL||'http://localhost:18080',headless:true},reporter:'list'})
