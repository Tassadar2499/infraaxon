import { defineStore } from 'pinia'
export const useWorkspace = defineStore('workspace', { state: () => ({ activeEnvironment: '', view: 'components' }) })
