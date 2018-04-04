import asyncio
import discord
import random
import json
import os
import io
import safygiphy
import requests
from discord.ext import commands

g = safygiphy.Giphy()
voz = True

if not discord.opus.is_loaded():
    discord.opus.load_opus('libopus.so')

class VoiceEntry:
    def __init__(self, message, player):
        self.requester = message.author
        self.channel = message.channel
        self.player = player

    def __str__(self):
        fmt = '*{0.title}* uploaded by {0.uploader} and requested by {1.display_name}'
        duration = self.player.duration
        if duration:
            fmt = fmt + ' [length: {0[0]}m {0[1]}s]'.format(divmod(duration, 60))
        return fmt.format(self.player, self.requester)

class VoiceState:
    def __init__(self, bot):
        self.current = None
        self.voice = None
        self.bot = bot
        self.play_next_song = asyncio.Event()
        self.songs = asyncio.Queue()
        self.skip_votes = set() # a set of user_ids that voted
        self.audio_player = self.bot.loop.create_task(self.audio_player_task())

    def is_playing(self):
        if self.voice is None or self.current is None:
            return False

        player = self.current.player
        return not player.is_done()

    @property
    def player(self):
        return self.current.player

    def skip(self):
        self.skip_votes.clear()
        if self.is_playing():
            self.player.stop()

    def toggle_next(self):
        self.bot.loop.call_soon_threadsafe(self.play_next_song.set)

    async def audio_player_task(self):
        while True:
            self.play_next_song.clear()
            self.current = await self.songs.get()
            await self.bot.send_message(self.current.channel, 'Now playing ' + str(self.current))
            self.current.player.start()
            await self.play_next_song.wait()

class Music:
    """Voice related commands.
    Works in multiple servers at once.
    """
    def __init__(self, bot):
        self.bot = bot
        self.voice_states = {}

    def get_voice_state(self, server):
        state = self.voice_states.get(server.id)
        if state is None:
            state = VoiceState(self.bot)
            self.voice_states[server.id] = state

        return state

    async def create_voice_client(self, channel):
        voice = await self.bot.join_voice_channel(channel)
        state = self.get_voice_state(channel.server)
        state.voice = voice

    def __unload(self):
        for state in self.voice_states.values():
            try:
                state.audio_player.cancel()
                if state.voice:
                    self.bot.loop.create_task(state.voice.disconnect())
            except:
                pass

    @commands.command(pass_context=True, no_pm=True)
    async def join(self, ctx, *, channel : discord.Channel):
        """Joins a voice channel."""
        try:
            await self.create_voice_client(channel)
        except discord.ClientException:
            await self.bot.say('Already in a voice channel...')
        except discord.InvalidArgument:
            await self.bot.say('This is not a voice channel...')
        else:
            await self.bot.say('Ready to play audio in ' + channel.name)

    @commands.command(pass_context=True, no_pm=True)
    async def summon(self, ctx):
        """Summons the bot to join your voice channel."""
        summoned_channel = ctx.message.author.voice_channel
        if summoned_channel is None:
            await self.bot.say('You are not in a voice channel.')
            return False

        state = self.get_voice_state(ctx.message.server)
        if state.voice is None:
            state.voice = await self.bot.join_voice_channel(summoned_channel)
        else:
            await state.voice.move_to(summoned_channel)

        return True

    @commands.command(pass_context=True, no_pm=True)
    async def play(self, ctx, *, song : str):
        """Plays a song.
        If there is a song currently in the queue, then it is
        queued until the next song is done playing.
        This command automatically searches as well from YouTube.
        The list of supported sites can be found here:
        https://rg3.github.io/youtube-dl/supportedsites.html
        """
        state = self.get_voice_state(ctx.message.server)
        opts = {
            'default_search': 'auto',
            'quiet': True,
        }

        if state.voice is None:
            success = await ctx.invoke(self.summon)
            if not success:
                return

        try:
            player = await state.voice.create_ytdl_player(song, ytdl_options=opts, after=state.toggle_next)
        except Exception as e:
            fmt = 'An error occurred while processing this request: ```py\n{}: {}\n```'
            await self.bot.send_message(ctx.message.channel, fmt.format(type(e).__name__, e))
        else:
            player.volume = 0.6
            entry = VoiceEntry(ctx.message, player)
            await self.bot.say('Enqueued ' + str(entry))
            await state.songs.put(entry)

    @commands.command(pass_context=True, no_pm=True)
    async def volume(self, ctx, value : int):
        """Sets the volume of the currently playing song."""

        state = self.get_voice_state(ctx.message.server)
        if state.is_playing():
            player = state.player
            player.volume = value / 100
            await self.bot.say('Set the volume to {:.0%}'.format(player.volume))

    @commands.command(pass_context=True, no_pm=True)
    async def pause(self, ctx):
        """Pauses the currently played song."""
        state = self.get_voice_state(ctx.message.server)
        if state.is_playing():
            player = state.player
            player.pause()

    @commands.command(pass_context=True, no_pm=True)
    async def resume(self, ctx):
        """Resumes the currently played song."""
        state = self.get_voice_state(ctx.message.server)
        if state.is_playing():
            player = state.player
            player.resume()

    @commands.command(pass_context=True, no_pm=True)
    async def stop(self, ctx):
        """Stops playing audio and leaves the voice channel.
        This also clears the queue.
        """
        server = ctx.message.server
        state = self.get_voice_state(server)

        if state.is_playing():
            player = state.player
            player.stop()

        try:
            state.audio_player.cancel()
            del self.voice_states[server.id]
            await state.voice.disconnect()
        except:
            pass
        
    @commands.command(pass_context=True, no_pm=True)
    async def leave(self, ctx):

        server = ctx.message.server
        state = self.get_voice_state(server)

        if not state.is_playing():
            player = state.player
            player.stop()

        if not state.is_playing():
            del self.voice_states[server.id]
            await state.voice.disconnect()

    @commands.command(pass_context=True, no_pm=True)
    async def skip(self, ctx):
        """Vote to skip a song. The song requester can automatically skip.
        3 skip votes are needed for the song to be skipped.
        """
        
        state = self.get_voice_state(ctx.message.server)
        if not state.is_playing():
            await self.bot.say('Not playing any music right now...')
            return

        voter = ctx.message.author
        if voter == state.current.requester:
            await self.bot.say('Requester requested skipping song...')
            state.skip()
        elif voter.id not in state.skip_votes:
            state.skip_votes.add(voter.id)
            total_votes = len(state.skip_votes)
            if total_votes >= 3:
                await self.bot.say('Skip vote passed, skipping song...')
                state.skip()
            else:
                await self.bot.say('Skip vote added, currently at [{}/3]'.format(total_votes))
        else:
            await self.bot.say('You have already voted to skip this song.')

    @commands.command(pass_context=True, no_pm=True)
    async def playing(self, ctx):
        """Shows info about the currently played song."""

        state = self.get_voice_state(ctx.message.server)
        if state.current is None:
            await self.bot.say('Not playing anything.')
        else:
            skip_count = len(state.skip_votes)
            await self.bot.say('Now playing {} [skips: {}/3]'.format(state.current, skip_count))

    @commands.command(pass_context=True, no_pm=True)
    async def move(self, ctx, member: discord.Member, channel: discord.Channel):
        await bot.move_member(member, channel)

    @commands.command(pass_context=True, no_pm=True)
    async def ohmaigad(self, ctx):
        global voz
        server = ctx.message.server
        state = self.get_voice_state(server)
        if state.voice is None:
            success = await ctx.invoke(self.summon)
            if not success:
                return
        try:
            if voz is True:
                player = state.voice.create_ffmpeg_player('./Audio/ohmaigad.mp3')
                player.start()
                while not player.is_done():
                    voz = False
                voz = True
        except:
            pass
        else:
            return

    @commands.command(pass_context=True, no_pm=True)
    async def sotelo(self, ctx):
        global voz
        server = ctx.message.server
        state = self.get_voice_state(server)
        if state.voice is None:
            success = await ctx.invoke(self.summon)
            if not success:
                return
        try:
            if voz is True:
                player = state.voice.create_ffmpeg_player('./Audio/sotelo.mp3')
                player.start()
                while not player.is_done():
                    voz = False
                voz = True
        except:
            pass
        else:
            return

    @commands.command(pass_context=True, no_pm=True)
    async def sotelo2(self, ctx):
        global voz
        server = ctx.message.server
        state = self.get_voice_state(server)
        if state.voice is None:
            success = await ctx.invoke(self.summon)
            if not success:
                return
        try:
            if voz is True:
                player = state.voice.create_ffmpeg_player('./Audio/sotelo2.mp3')
                player.start()
                while not player.is_done():
                    voz = False
                voz = True
        except:
            pass
        else:
            return

    @commands.command(pass_context=True, no_pm=True)
    async def sostenlo(self, ctx):
        global voz
        server = ctx.message.server
        state = self.get_voice_state(server)
        if state.voice is None:
            success = await ctx.invoke(self.summon)
            if not success:
                return
        try:
            if voz is True:
                player = state.voice.create_ffmpeg_player('./Audio/sostenlo.mp3')
                player.start()
                while not player.is_done():
                    voz = False
                voz = True
        except:
            pass
        else:
            return

    @commands.command(pass_context=True, no_pm=True)
    async def fonsi(self, ctx):
        global voz
        server = ctx.message.server
        state = self.get_voice_state(server)
        if state.voice is None:
            success = await ctx.invoke(self.summon)
            if not success:
                return
        try:
            if voz is True:
                player = state.voice.create_ffmpeg_player('./Audio/fonsi.mp3')
                player.start()
                while not player.is_done():
                    voz = False
                voz = True
        except:
            pass
        else:
            return
    
    @commands.command(pass_context=True, no_pm=True)
    async def agusto(self, ctx):
        global voz
        server = ctx.message.server
        state = self.get_voice_state(server)
        if state.voice is None:
            success = await ctx.invoke(self.summon)
            if not success:
                return
        try:
            if voz is True:
                player = state.voice.create_ffmpeg_player('./Audio/pacheco.mp3')
                player.start()
                while not player.is_done():
                    voz = False
                voz = True
        except:
            pass
        else:
            return
    
    @commands.command(pass_context=True, no_pm=True)
    async def pacheco(self, ctx):
        global voz
        server = ctx.message.server
        state = self.get_voice_state(server)
        if state.voice is None:
            success = await ctx.invoke(self.summon)
            if not success:
                return
        try:
            if voz is True:
                player = state.voice.create_ffmpeg_player('./Audio/agusto.mp3')
                player.start()
                while not player.is_done():
                    voz = False
                voz = True
        except:
            pass
        else:
            return
    
    @commands.command(pass_context=True, no_pm=True)
    async def jalo(self, ctx):
        global voz
        server = ctx.message.server
        state = self.get_voice_state(server)
        if state.voice is None:
            success = await ctx.invoke(self.summon)
            if not success:
                return
        try:
            if voz is True:
                player = state.voice.create_ffmpeg_player('./Audio/jalo.mp3')
                player.start()
                while not player.is_done():
                    voz = False
                voz = True
        except:
            pass
        else:
            return

    @commands.command(pass_context=True, no_pm=True)
    async def pacheco2(self, ctx):
        global voz
        server = ctx.message.server
        state = self.get_voice_state(server)
        if state.voice is None:
            success = await ctx.invoke(self.summon)
            if not success:
                return
        try:
            if voz is True:
                player = state.voice.create_ffmpeg_player('./Audio/pacheco2.mp3')
                player.start()
                while not player.is_done():
                    voz = False
                voz = True
        except:
            pass
        else:
            return

bot = commands.Bot(command_prefix=commands.when_mentioned_or('$'), description='A playlist example for discord.py')
bot.add_cog(Music(bot))

@bot.event
async def on_ready():
    print('Logged in as:\n{0} (ID: {0.id})'.format(bot.user))
    await bot.change_presence(game=discord.Game(name="Pollo"))

@bot.event
async def on_message(message):
    if message.content.startswith('!guardarfrase'):
        if not os.path.isfile("./Frases/frase_file.pk1"):
            frase_list = []
        else:
            with open("./Frases/frase_file.pk1" , "r") as frase_file:
               frase_list = json.load(frase_file)
        frase_list.append(message.content[13:])
        with open("./Frases/frase_file.pk1" , "w") as frase_file:
            json.dump(frase_list, frase_file)
    elif message.content.startswith('!frase'):
        with open("./Frases/frase_file.pk1" , "r") as frase_file:
                frase_list = json.load(frase_file)
        await bot.send_message(message.channel ,  random.choice(frase_list))

    if message.content.startswith('!kiss'):
        response = requests.get("https://media.giphy.com/media/fBS8d3MublSPmrb3Ys/giphy.gif", stream=True)
        await bot.send_file(message.channel, io.BytesIO(response.raw.read()), filename='kiss.gif', content='Sotelo kiss Gif.')

    if message.content.startswith('!sotelo'):
        response = requests.get("https://media.giphy.com/media/C8975W8loq6omiX8QC/giphy.gif", stream=True)
        await bot.send_file(message.channel, io.BytesIO(response.raw.read()), filename='ganzo.gif', content='Aahh aaah Soteloo! Gif.')
    
    if message.content.startswith('!help'):
        await bot.send_message(message.channel, 'Comandos:\n!help\n@Hector-Lagarda stop\nFrases:\n!frase\nGifs:\n!kiss\n!sotelo\nAudios:\n@Hector-Lagarda sotelo\n@Hector-Lagarda sotelo2\n@Hector-Lagarda sostenlo\n@Hector-Lagarda fonsi\n@Hector-Lagarda ohmaigad\n@Hector-Lagarda pacheco\n@Hector-Lagarda agusto\n@Hector-Lagarda jalo\n@Hector-Lagarda pacheco2')
    await bot.process_commands(message)    

bot.run('NDI5MzgxOTU0MzU0NTQ0NjUw.DaFTeg.O_4Co5p9IdBTHwqg3p7VoHklMQQ')
