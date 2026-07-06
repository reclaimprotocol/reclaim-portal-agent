"use client";
import { createContext, useContext, useState } from "react";
import UniversityProfile from "./UniversityProfile";

// Lets any component open the shared University Profile modal via useProfile().
const ProfileCtx = createContext(() => {});
export function useProfile() { return useContext(ProfileCtx); }

export default function ProfileProvider({ children }) {
  const [org, setOrg] = useState(null);
  return (
    <ProfileCtx.Provider value={setOrg}>
      {children}
      {org && <UniversityProfile orgid={org} onClose={() => setOrg(null)} />}
    </ProfileCtx.Provider>
  );
}
