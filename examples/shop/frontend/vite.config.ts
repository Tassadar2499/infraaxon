import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
export default defineConfig({plugins:[vue()],server:{proxy:{'/api/products':'http://localhost:18101','/api/orders':'http://localhost:18102'}}})
