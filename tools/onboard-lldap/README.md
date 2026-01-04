# Onboard LLDAP

A Python tool to setup new users in LLDAP and send them an email with instructions

## Installation

Install the required dependencies:

```bash
pip install -r requirements.txt
```

## Usage

The easiest way to use the tool is to have all the env variables in 1password and use `onboard.sh` 

```
./onboard.sh --username test1 --email "test1@gmail.com" --first-name "test" --last-name "user"
```

You can also look in `env.template` for all the variables it expents to exist and export those some other way if you wish.

## Requirements

The following external commands must be available:
- `op` (1Password CLI) - if using onboard.sh
