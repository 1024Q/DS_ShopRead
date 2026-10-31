'use client';
import { createContext, useContext } from 'react';
export const AdminWorkspaceContext = createContext(true);
export const useAdminWorkspace = () => useContext(AdminWorkspaceContext);
