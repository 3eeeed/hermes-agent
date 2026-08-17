// Building a context menu is asynchronous (it queries the renderer's DOM at
// the click point), so two rapid right-clicks can finish out of order and pop
// the older target's menu — acting on the wrong file. Each build takes a
// generation number and only shows its menu while it is still the newest.
export interface ContextMenuSequencer {
  run: (build: (isCurrent: () => boolean) => Promise<void>) => Promise<void>
}

export function createContextMenuSequencer(): ContextMenuSequencer {
  let generation = 0

  return {
    run: async build => {
      const current = ++generation

      await build(() => current === generation)
    }
  }
}
