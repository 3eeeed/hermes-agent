import { contextFileSourceLink, type FileLinkContextMenuModel, type FileLinkMenuItemId } from './file-link-context-menu'

export interface ContextFileActionDependencies {
  copyFile: (filePath: string) => Promise<void>
  copyText: (value: string) => void
  materialize: (model: FileLinkContextMenuModel) => Promise<string>
  openFile: (filePath: string) => Promise<void>
  revealFile: (filePath: string) => void
}

export async function runContextFileAction(
  action: FileLinkMenuItemId,
  model: FileLinkContextMenuModel,
  dependencies: ContextFileActionDependencies
): Promise<void> {
  if (action === 'copy-link') {
    dependencies.copyText(contextFileSourceLink(model.source))

    return
  }

  const localPath = await dependencies.materialize(model)

  if (action === 'copy-path') {
    dependencies.copyText(localPath)

    return
  }

  if (action === 'copy-file') {
    await dependencies.copyFile(localPath)

    return
  }

  if (action === 'open-file') {
    await dependencies.openFile(localPath)

    return
  }

  if (action === 'reveal-file') {
    dependencies.revealFile(localPath)

    return
  }

  throw new Error(`Context file action is not implemented: ${action}`)
}
