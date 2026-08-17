import assert from 'node:assert/strict'

import { test } from 'vitest'

import { copyFileToClipboard, fileClipboardPlan } from './native-file-clipboard'

test('executes a command plan without writing a Linux clipboard buffer', async () => {
  const commands: Array<{ args: string[]; command: string }> = []
  let bufferWrites = 0

  await copyFileToClipboard('/Users/ahmed/review.pdf', 'darwin', {
    runCommand: async (command, args) => {
      commands.push({ args, command })
    },
    writeBuffer: () => {
      bufferWrites += 1
    }
  })

  assert.equal(commands.length, 1)
  assert.equal(commands[0]?.command, '/usr/bin/osascript')
  assert.equal(bufferWrites, 0)
})

test('executes a Linux buffer plan without spawning a command', async () => {
  const writes: Array<{ data: Buffer; format: string }> = []
  let commands = 0

  await copyFileToClipboard('/home/ahmed/review.pdf', 'linux', {
    runCommand: async () => {
      commands += 1
    },
    writeBuffer: (format, data) => {
      writes.push({ data, format })
    }
  })

  assert.equal(commands, 0)
  assert.equal(writes.length, 1)
  assert.equal(writes[0]?.format, 'text/uri-list')
  assert.equal(writes[0]?.data.toString('utf8'), 'file:///home/ahmed/review.pdf\r\n')
})

test('macOS passes the file path as a separate AppleScript argument', () => {
  const filePath = `/Users/ahmed/Factory Review's Final.pdf`
  const plan = fileClipboardPlan(filePath, 'darwin')

  assert.equal(plan.kind, 'command')
  assert.equal(plan.command, '/usr/bin/osascript')
  assert.equal(plan.args.at(-1), filePath)
  assert.equal(plan.args.slice(0, -1).some(argument => argument.includes(filePath)), false)
  assert.equal(plan.args.includes('--'), true)
})

test('Windows builds an STA file-drop clipboard command with an escaped path', () => {
  const filePath = `C:\\Users\\Ahmed\\O'Brien.pdf`
  const plan = fileClipboardPlan(filePath, 'win32')

  assert.equal(plan.kind, 'command')
  assert.equal(plan.command, 'powershell.exe')
  assert.equal(plan.args.includes('-STA'), true)
  assert.equal(plan.args.includes('-EncodedCommand'), true)

  const encoded = plan.args.at(-1)
  assert.ok(encoded)
  const script = Buffer.from(encoded, 'base64').toString('utf16le')

  assert.match(script, /SetFileDropList/)
  assert.match(script, /O''Brien\.pdf/)
  assert.equal(script.includes(`O'Brien.pdf`), false)
})

test('Linux writes a standards-based file URI list', () => {
  const plan = fileClipboardPlan('/home/ahmed/Review Package.zip', 'linux')

  assert.equal(plan.kind, 'buffer')
  assert.equal(plan.format, 'text/uri-list')
  assert.equal(plan.data.toString('utf8'), 'file:///home/ahmed/Review%20Package.zip\r\n')
})

test('GNOME-family desktops receive the copied-files clipboard format', () => {
  const plan = fileClipboardPlan('/home/ahmed/review.pdf', 'linux', { desktop: 'ubuntu:GNOME' })

  assert.equal(plan.kind, 'buffer')
  assert.equal(plan.format, 'x-special/gnome-copied-files')
  assert.equal(plan.data.toString('utf8'), 'copy\nfile:///home/ahmed/review.pdf\n')
})
