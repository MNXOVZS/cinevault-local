import type { ExperienceBridge, PlayerMountOptions, ExperienceMedia, CardPreviewItem, CardPreviewOptions } from './contracts';
import { CinemaPlayer } from './player';
import { CardPreviewManager } from './previews';
import { AnalysisManager } from './analysis';
import { LibraryManagement, CacheManager } from './management';
import { LibraryHealthManager } from './library-health';

let player: CinemaPlayer | null = null;
const previews = new CardPreviewManager();
let analysis: AnalysisManager | null = null;
let management: LibraryManagement | null = null;
let cache: CacheManager | null = null;
let health: LibraryHealthManager | null = null;
const bridge: ExperienceBridge = {
  mountPlayer(options: PlayerMountOptions): void { player?.dispose(); player = new CinemaPlayer(options); },
  syncPlayer(item: ExperienceMedia): void { player?.update(item); },
  unmountPlayer(): void { player?.dispose(); player = null; },
  syncCards(root: HTMLElement, items: readonly CardPreviewItem[], options: CardPreviewOptions): void { previews.mount(root, items, options); },
  stopCards(): void { previews.stop(); },
  suspendCards(): void { previews.suspend(); },
  mountAnalysis(root, onMessage): void { analysis?.dispose(); analysis = new AnalysisManager(root, onMessage); },
  refreshAnalysis(): void { analysis?.refresh(); },
  unmountAnalysis(): void { analysis?.dispose(); analysis = null; },
  mountManagement(root, onMessage): void { management?.dispose(); management = new LibraryManagement(root, onMessage, () => analysis?.refresh()); },
  unmountManagement(): void { management?.dispose(); management = null; },
  mountCache(root, onMessage): void { cache?.dispose(); cache = new CacheManager(root, onMessage, () => previews.resetPreparation()); },
  unmountCache(): void { cache?.dispose(); cache = null; },
  mountHealth(root, options): void { health?.dispose(); health = new LibraryHealthManager(root, options); },
  refreshHealth(): void { void health?.refresh(); },
  unmountHealth(): void { health?.dispose(); health = null; },
};
window.CineVaultExperience = bridge;
