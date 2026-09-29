// APPROXIMATE, DISPLAY-ONLY positions (percent of the map canvas). The simulator exposes no coordinates or
// distances (spec C20); these only place the markers roughly where the real towns are (Dhaka north, Chattogram south-east).
export const POS: Record<string, { x: number; y: number }> = {
  "depot-gazipur": { x: 30, y: 16 },
  "station-tongi": { x: 20, y: 30 },
  "station-mirpur": { x: 36, y: 40 },
  "depot-patiya": { x: 70, y: 66 },
  "station-karnaphuli": { x: 60, y: 56 },
  "station-coxsbazar": { x: 82, y: 88 },
};
