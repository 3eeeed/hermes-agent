import { pathToFileURL } from 'node:url'

import { encodePowerShellCommand } from './wsl-clipboard-image'

export type NativeFileClipboardPlatform = 'darwin' | 'linux' | 'win32'

export interface FileClipboardBufferPlan {
  data: Buffer
  format: string
  kind: 'buffer'
}

export interface FileClipboardCommandPlan {
  args: string[]
  command: string
  kind: 'command'
}

export interface FileClipboardPlanOptions {
  desktop?: string
}

export interface FileClipboardRuntime {
  runCommand: (command: string, args: string[]) => Promise<void>
  writeBuffer: (format: string, data: Buffer) => Promise<void> | void
}

export type FileClipboardPlan = FileClipboardBufferPlan | FileClipboardCommandPlan

export async function copyFileToClipboard(
  filePath: string,
  platform: NativeFileClipboardPlatform,
  runtime: FileClipboardRuntime,
  options: FileClipboardPlanOptions = {}
): Promise<void> {
  const plan = fileClipboardPlan(filePath, platform, options)

  if (plan.kind === 'buffer') {
    await runtime.writeBuffer(plan.format, plan.data)

    return
  }

  await runtime.runCommand(plan.command, plan.args)
}

export function fileClipboardPlan(
  filePath: string,
  platform: NativeFileClipboardPlatform,
  options: FileClipboardPlanOptions = {}
): FileClipboardPlan {
  if (platform === 'linux') {
    const fileUrl = pathToFileURL(filePath).toString()
    const gnomeFamily = /(?:cinnamon|gnome|mate|unity)/i.test(options.desktop || '')

    return gnomeFamily
      ? {
          kind: 'buffer',
          format: 'x-special/gnome-copied-files',
          data: Buffer.from(`copy\n${fileUrl}\n`, 'utf8')
        }
      : {
          kind: 'buffer',
          format: 'text/uri-list',
          data: Buffer.from(`${fileUrl}\r\n`, 'utf8')
        }
  }

  if (platform === 'win32') {
    const escapedPath = filePath.replace(/'/g, "''")

    const script = [
      'Add-Type -AssemblyName System.Windows.Forms',
      '$files = New-Object System.Collections.Specialized.StringCollection',
      `[void]$files.Add('${escapedPath}')`,
      '[System.Windows.Forms.Clipboard]::SetFileDropList($files)'
    ].join('\n')

    return {
      kind: 'command',
      command: 'C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe',
      args: [
        '-NoProfile',
        '-NonInteractive',
        '-STA',
        '-ExecutionPolicy',
        'Bypass',
        '-EncodedCommand',
        encodePowerShellCommand(script)
      ]
    }
  }

  if (platform !== 'darwin') {
    throw new Error(`File clipboard is not implemented for ${platform}`)
  }

  return {
    kind: 'command',
    command: '/usr/bin/osascript',
    args: [
      '-e',
      'on run argv',
      '-e',
      'set the clipboard to (POSIX file (item 1 of argv))',
      '-e',
      'end run',
      '--',
      filePath
    ]
  }
}
