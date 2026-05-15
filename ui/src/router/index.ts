import { createRouter, createWebHashHistory, type RouteLocationNormalized } from 'vue-router';
import ChatView from '../views/ChatView.vue';
import { useSettingsStore } from '../stores/settings';
import { PROFILE_ROUTES } from './profileRoutes';

const routes = [
  // Root: profile selector (or redirect to setup if first time)
  {
    path: '/',
    name: 'home',
    component: () => import('../views/ProfileSelector.vue'),
  },
  // Setup routes
  {
    path: '/setup',
    name: 'setup',
    component: () => import('../views/SetupWizard.vue'),
  },
  {
    path: '/setup/:profile',
    name: 'setup-profile',
    component: () => import('../views/SetupWizard.vue'),
    props: true,
  },
  // Login route for a specific profile
  {
    path: '/login/:profile',
    name: 'login',
    component: () => import('../views/LoginPage.vue'),
    props: true,
  },
  // Profile-scoped routes
  {
    path: '/:profile',
    name: 'chat',
    component: ChatView,
    props: true,
  },
  {
    path: '/:profile/c/:conversationId',
    name: 'conversation',
    component: ChatView,
    props: true,
  },
  {
    path: '/:profile/settings',
    name: 'settings',
    component: () => import('../views/SettingsPage.vue'),
    props: true,
  },
  {
    path: '/:profile/settings/llm',
    name: 'llm-settings',
    component: () => import('../views/LLMSettings.vue'),
    props: true,
  },
  {
    path: '/:profile/settings/tools-skills',
    name: 'tools-skills-settings',
    component: () => import('../views/AgentsToolsSettings.vue'),
    props: true,
  },
  {
    path: '/:profile/settings/config',
    name: 'user-config-settings',
    component: () => import('../views/UserConfigSettings.vue'),
    props: true,
  },
  {
    path: '/:profile/settings/embedding',
    name: 'embedding-settings',
    component: () => import('../views/EmbeddingSettings.vue'),
    props: true,
    // Vector Embedding is a server-wide configuration owned by the
    // admin profile. Backend already enforces this with require_admin,
    // but block the navigation up-front so non-admin users never see
    // a 403'd page.
    beforeEnter: (to: RouteLocationNormalized) => {
      const profile = to.params.profile as string | undefined;
      if (profile && profile !== 'admin') {
        return { path: `/${profile}/settings`, replace: true };
      }
      return true;
    },
  },
  {
    path: '/:profile/settings/profiles',
    name: 'profile-settings',
    component: () => import('../views/ProfileSettings.vue'),
    props: true,
  },
  {
    path: '/:profile/settings/channels',
    name: 'channels-settings',
    component: () => import('../views/ChannelsSettings.vue'),
    props: true,
  },
  {
    path: '/:profile/settings/updates',
    name: 'updates-settings',
    component: () => import('../views/UpdatesSettings.vue'),
    props: true,
  },
  {
    path: '/:profile/channels',
    name: 'channels-page',
    component: () => import('../views/ChannelsPage.vue'),
    props: true,
  },
  // Process Manager — long-running exec_shell processes
  {
    path: '/:profile/processes',
    name: 'process-list',
    component: () => import('../views/ProcessList.vue'),
    props: true,
  },
  {
    path: '/:profile/processes/:pid',
    name: 'process-terminal',
    component: () => import('../views/ProcessTerminal.vue'),
    props: true,
  },
  // Skill Events — conversation-scoped event subscriptions
  {
    path: '/:profile/events',
    name: 'skill-events',
    component: () => import('../views/SkillEventsPage.vue'),
    props: true,
  },
];

const router = createRouter({
  history: createWebHashHistory(),
  routes,
});

// First-run gate (Electron only). When the runtime config has no agent
// URL — typical for a fresh install of the Electron app — every route
// other than ``/setup`` redirects there. The Setup Wizard hosts the
// first-run installer phase inline (see SetupWizard.vue's
// ``includeInstallerSteps``) so the user moves through Welcome →
// Deployment → Mode → Install → Server → … in a single continuous
// flow. We deliberately don't apply this in the web build (no
// ``window.openpa``) because there the agent URL comes from
// VITE_AGENT_URL at build time.
//
// ``bridge.config.agentUrl`` is the snapshot the Electron preload
// captured at startup; it never refreshes mid-session even after
// ``setAgentUrl`` writes the new value through to disk. To avoid
// bouncing post-setup navigation back to /setup when the bridge
// snapshot is stale, consult the live Pinia ref as a fallback — it's
// the same source ``a2aClient.getBaseUrl`` uses.
router.beforeEach((to) => {
  const bridge = window.openpa
  if (!bridge) return true
  if (bridge.config.agentUrl) return true
  if (useSettingsStore().agentUrl) return true
  if (to.name === 'setup' || to.name === 'setup-profile') return true
  return { path: '/setup', replace: true }
});

// Synchronously activate the per-profile auth token before any view renders.
// Without this, child views' onMounted fires API calls before App.vue's
// onMounted has a chance to load the token (parent onMounted runs after
// children in Vue 3), producing 401s on hard reloads of profile-scoped pages.
router.beforeEach((to) => {
  const routeName = typeof to.name === 'string' ? to.name : '';
  const profile = (to.params.profile as string | undefined) || '';
  if (!routeName || !PROFILE_ROUTES.has(routeName) || !profile) return true;

  const settingsStore = useSettingsStore();
  const token = settingsStore.getTokenForProfile(profile);
  if (!token) {
    return { path: `/login/${profile}`, replace: true };
  }
  settingsStore.activateProfile(profile);
  return true;
});

export default router;
