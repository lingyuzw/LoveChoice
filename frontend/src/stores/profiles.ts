import { defineStore } from "pinia";
import { createBotProfile, deleteBotProfile, loadBotProfiles, updateBotProfile, type BotProfile } from "@/api/profiles";

interface ProfilesState {
  profiles: BotProfile[];
  loading: boolean;
  saving: boolean;
  error: string;
}

export const useProfilesStore = defineStore("profiles", {
  state: (): ProfilesState => ({
    profiles: [],
    loading: false,
    saving: false,
    error: "",
  }),
  actions: {
    async reload() {
      this.loading = true;
      this.error = "";
      try {
        this.profiles = (await loadBotProfiles()).profiles || [];
      } catch (error) {
        this.error = error instanceof Error ? error.message : String(error);
      } finally {
        this.loading = false;
      }
    },
    async add(system = "") {
      const id = `profile_${Date.now().toString(36)}`;
      const data = await createBotProfile({ id, name: "新人格", system, reply_style: "natural", tools_enabled: true });
      this.profiles = data.profiles || [];
    },
    async saveAll() {
      this.saving = true;
      try {
        for (const profile of this.profiles) {
          const data = await updateBotProfile(profile.id, profile);
          this.profiles = data.profiles || this.profiles;
        }
      } finally {
        this.saving = false;
      }
    },
    async remove(id: string) {
      if (!id || id === "default") return;
      const data = await deleteBotProfile(id);
      this.profiles = data.profiles || [];
    },
  },
});
