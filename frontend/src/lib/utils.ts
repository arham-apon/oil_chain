import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";
export const cn = (...i: ClassValue[]) => twMerge(clsx(i));
export const fmtL = (v?: number | null) => (v == null ? "—" : `${Math.round(v).toLocaleString()} L`);
export const pct = (v?: number | null, d = 1) => (v == null ? "—" : `${(v * 100).toFixed(d)}%`);
export const FUELS = ["DIESEL", "PETROL", "OCTANE"] as const;
