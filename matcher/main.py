from redbot.core import commands, Config
from redbot.core.bot import Red
from redbot.core.utils import chat_formatting as cf
from redbot.core.utils.predicates import MessagePredicate
from redbot.core.data_manager import cog_data_path
import discord
import asyncio
from dataclasses import dataclass, asdict
import typing
import aiohttp
import io
import time
from PIL import Image
from datetime import datetime, timezone, timedelta
from discord.ext import tasks


@dataclass
class Question:
    question: str
    preset_options: list
    key: str


@dataclass
class UserMatch:
    user_id: int
    private_channel: int
    matched_at: int


class CommaSeparatedList(commands.Converter):
    async def convert(self, ctx: commands.Context, argument: str):
        if not "," in argument:
            raise commands.BadArgument()
        return argument.split(",")


GUILD_DEFAULTS = {
    "bio_channel": {
        "male": {"casual": None, "serious": None, "bff": None},
        "female": {"casual": None, "serious": None, "bff": None},
        "other": {"casual": None, "serious": None, "bff": None},
    },
    "gender_status_roles": dict.fromkeys(
        (
            "male_serious",
            "male_casual",
            "male_bff",
            "female_serious",
            "female_casual",
            "female_bff",
            "other_serious",
            "other_casual",
            "other_bff",
        )
    ),
    "questions": [],
}

MEMBER_DEFAULTS = {
    "registered": False,
    "bio": {},
    "profile_picture": None,
    "bio_channel": None,
    "bio_message": None,
    "matches": [],
}

_spanish_equivalents = {
    "hombre": "male",
    "mujer": "female",
    "otrox": "other",
    "casual": "casual",
    "serio": "serious",
    "amigos": "bff",
}


class Matcher(commands.Cog):
    def __init__(self, bot: Red):
        self.bot = bot
        self.bio_channels = {}
        self.config = Config.get_conf(self, identifier=1234567890)

        self.config.register_guild(**GUILD_DEFAULTS)
        self.config.register_member(**MEMBER_DEFAULTS)
        asyncio.create_task(self.populate_cache())

    async def populate_cache(self):
        all_guilds = await self.config.all_guilds()

        self.bio_channels = {
            gid: [
                guild["bio_channel"][gender][key]
                for gender in guild["bio_channel"]
                for key in guild["bio_channel"][gender]
            ]
            for gid, guild in all_guilds.items()
        }

    def create_bio_embed(self, member: discord.Member, bio: dict):
        embed = discord.Embed(
            title=f"{member.display_name}'s ({member} {member.id}) bio",
            color=member.color,
        )

        for key, answer in bio.items():
            embed.add_field(name=key, value=answer, inline=False)

        embed.set_thumbnail(url="attachment://profile_pic.png")

        return embed

    async def update_bio(self, member: discord.Member, bio):
        channel = member.guild.get_channel(await self.config.member(member).bio_channel())
        message = await channel.fetch_message(await self.config.member(member).bio_message())

        await message.edit(embed=self.create_bio_embed(member, bio))

    @tasks.loop(hours=1)
    async def check_expired_matches(self):
        for guild, guild_data in await self.config.all_guilds():
            for member_id, member_data in guild_data["members"].items():
                for match in member_data["matches"].copy():
                    if (
                        datetime.from_timestamp(match["matched_at"]) + timedelta(days=3)
                        <= datetime.now()
                    ):
                        channel = self.bot.get_guild(guild).get_channel(match["private_channel"])
                        if not channel:
                            member_data["matches"].remove(match)
                            continue
                        try:
                            await channel.delete()

                        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                            pass

                        member_data["matches"].remove(match)

    @check_expired_matches.before_loop
    async def before_check_expired_matches(self):
        await self.bot.wait_until_red_ready()

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.channel_id not in self.bio_channels[payload.guild_id]:
            return

        if not str(payload.emoji) == "✅":
            return

        if not await self.config.member_from_ids(payload.guild_id, payload.user_id).registered():
            return

        guild = self.bot.get_guild(payload.guild_id)
        channel = guild.get_channel(payload.channel_id)

        user = self.get_user_from_bio_message(
            message := await channel.fetch_message(payload.message_id)
        )

        if not user:
            await message.delete()
            return

        if user.id == payload.user_id:
            return

        private_channel = await self.bot.get_guild(payload.guild_id).create_text_channel(
            f"match-{payload.user_id}-{user.id}",
            overwrites={
                user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
                guild: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            },
        )
        await private_channel.send(
            f"{user.mention} {payload.member.mention}, match made!\nThis channel will expire in 3 days. If you want to, you can continue in dms after that."
        )

        async with self.config.member_from_ids(
            payload.guild_id, payload.user_id
        ).matches() as matches:
            matches.append(
                asdict(
                    UserMatch(
                        user.id,
                        private_channel.id,
                        matched_at=datetime.now(timezone.utc).timestamp(),
                    )
                )
            )

    def get_user_from_bio_message(self, message: discord.Message):
        return message.guild.get_member_named(message.embeds[0].title.split("'s")[0])

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.display_name != after.display_name:
            async with self.config.member(after).bio() as bio:
                bio["display name"] = after.display_name

                await self.update_bio(after, bio)

        if before.roles != after.roles:
            roles = await self.config.guild(after.guild).gender_status_roles()

            if len(set(after._roles).intersection(roles.values())) > 1:
                role_tr = (
                    set(after._roles).intersection(roles.values()).intersection(before._roles)
                )
                role_tr = [after.guild.get_role(role) for role in role_tr]
                await after.remove_roles(*role_tr)

            if not set(after._roles).difference(before._roles).intersection(roles.values()):
                return

            if await self.config.member(after).registered():
                return

            async with self.config.member(after).bio() as bio:
                for question in await self.config.guild(after.guild).questions():
                    answer = await self.ask_question(after, question)
                    if answer is None:
                        return

                    bio[question["key"]] = answer

            message = await after.send(
                "Please send a profile picture. It can be a link or an attachment."
            )

            try:
                message = await self.bot.wait_for(
                    "message",
                    check=MessagePredicate.same_context(channel=after.dm_channel, user=after),
                    timeout=60,
                )

            except asyncio.TimeoutError:
                return await after.send("You took too long to respond.")

            image = await self.get_image(after.guild, message)

            if not image:
                return await after.send("Invalid image.")

            await self.config.member(after).profile_picture.set(image)

            await self.config.member(after).registered.set(True)

            role = list(roles.keys())[
                list(roles.values()).index(set(after._roles).difference(before._roles).pop())
            ]
            gender, status = role.split("_")

            channel = after.guild.get_channel(
                await self.config.guild(after.guild)
                .bio_channel.get_attr(gender)
                .get_attr(status)()
            )
            if not channel:
                return

            message = await channel.send(
                embed=self.create_bio_embed(after, bio),
                file=discord.File(image, filename="profile_pic.png"),
            )

            await message.add_reaction("✅")

            await self.config.member(after).bio_channel.set(channel.id)
            await self.config.member(after).bio_message.set(message.id)

    @commands.group(name="matcherset", aliases=["matchersettings", "mset"])
    @commands.guild_only()
    @commands.admin()
    async def matcherset(self, ctx: commands.Context):
        """Matcher settings."""
        pass

    @matcherset.command(name="addquestion", aliases=["addq"])
    async def ms_addquestion(
        self,
        ctx: commands.Context,
        key: str,
        preset_options: typing.Optional[CommaSeparatedList] = None,
        *,
        question: str,
    ):
        """Add a question to the bio.

        Key is the way to represent the question. For example, if the question is "What is your favorite color?", the key could be "favourite color".
        if key is multiple words, it must be surrounded by quotes.

        preset_options is a comma-separated list of options. For example, if the question is "What is your favorite color?", the preset options could be "red,blue,green,yellow".
        if there is only one option, add a trailing comma to it. Notice how there are no spaces between the options.

        question is the question itself. For example, "What is your favorite color?".
        """
        async with self.config.guild(ctx.guild).questions() as questions:
            questions.append(asdict(Question(question, preset_options, key)))
        await ctx.send("Question added.")

    @matcherset.command(name="removequestion", aliases=["removeq"])
    async def ms_removequestion(self, ctx: commands.Context, *, key: str):
        """Remove a question from the bio.

        Key is the key used to add the question."""
        async with self.config.guild(ctx.guild).questions() as questions:
            for question in questions:
                if question["key"] == key:
                    questions.remove(question)
                    await ctx.send("Question removed.")
                    return
        await ctx.send("Question not found.")

    @matcherset.command(name="clearquestions", usage="", aliases=["clearq"])
    async def ms_clearquestions(self, ctx: commands.Context, *, confirm: bool = False):
        """Clear all questions."""
        if not confirm:
            return await ctx.send(
                "Are you sure you want to clear all questions? This action cannot be undone. If you are sure, run this command again `{ctx.prefix}matcherset clearquestions True`."
            )
        await self.config.guild(ctx.guild).questions.clear()
        await ctx.send("Questions cleared.")

    @matcherset.command(name="setbiochannel", aliases=["setbiochan", "sbc"])
    async def ms_setbiochannel(
        self,
        ctx: commands.Context,
        gender: commands.Literal["male", "female", "other"],
        status: commands.Literal["casual", "serious", "bff"],
        channel: discord.TextChannel,
    ):
        """Set channel where bio of a user will be sent.

        gender can be one of male, female or other."""
        await self.config.guild(ctx.guild).bio_channel.get_attr(gender).get_attr(status).set(
            channel.id
        )
        await ctx.send("Channel set.")

    @matcherset.command(name="setrole", aliases=["sr"])
    async def ms_setrole(
        self,
        ctx: commands.Context,
        gender: commands.Literal["male", "female", "other"],
        status: commands.Literal["casual", "serious", "bff"],
        role: discord.Role,
    ):
        """Set gender roles that are available to the user

        gender can be one of male, female or other
        status can be one of casual, serious or bff"""
        await self.config.guild(ctx.guild).gender_status_roles.set_raw(
            f"{gender}_{status}", value=role.id
        )
        await ctx.send("Role set.")

    @commands.command(name="updatebio")
    @commands.guild_only()
    @commands.cooldown(1, 60, commands.BucketType.user)
    async def updatebio(self, ctx: commands.Context):
        """Set your bio."""
        # Ask all the questions that have been answered before with the option to be able to skip them.
        # not answered questions will also be asked but they cannot be skipped.
        # ask them in dms.
        # if the question has preset options, send them as a list of reactions.
        # if the question doesn't have preset options, ask for the answer in chat.

        async with self.config.guild(ctx.guild).questions() as questions:
            async with self.config.member(ctx.author).bio() as bio:
                for question in questions:
                    if question["key"] in bio:
                        msg = await ctx.send(
                            f"Question: {question['question']}\nAnswer: {bio[question['key']]}\nDo you want to change this answer? (y/n)"
                        )
                        try:
                            response = await self.bot.wait_for(
                                "message",
                                check=lambda m: m.author == ctx.author
                                and m.channel == ctx.channel,
                                timeout=60,
                            )
                        except asyncio.TimeoutError:
                            await msg.edit(content="Timed out.")
                            return
                        if response.content.lower() == "y":
                            answer = await self.ask_question(ctx.author, question)
                            bio[question["key"]] = answer
                        elif response.content.lower() == "n":
                            pass
                        else:
                            await ctx.send("Invalid input.")
                            return
                    else:
                        answer = await self.ask_question(ctx.author, question)
                        bio[question["key"]] = answer

        await ctx.send("Do you want to change your profile picture too? (y/n)")

        pred = MessagePredicate.yes_or_no(ctx)
        try:
            msg = await self.bot.wait_for("message", check=pred, timeout=60)

        except asyncio.TimeoutError:
            await ctx.send("Timed out.")
            return

        if pred.result:
            image = await self.get_image(msg)
            if not image:
                return await ctx.send("Invalid image.")

            await self.config.member(ctx.author).profile_picture.set(image)

        await ctx.send("Bio updated.")

    async def ask_question(self, member: discord.Member, question: dict):
        if question["preset_options"]:
            newline = "\n"
            msg = await member.send(
                f"Question: {question['question']}\nOptions: {newline.join(enumerate(question['preset_options'], 1))}\nAnswer: \nPlease reply with the number of your answer."
            )
            pred = lambda x: (p := MessagePredicate.valid_int(user=member))(
                x
            ) and p.result in range(1, len(question["preset_options"]) + 1)
            try:
                message = await self.bot.wait_for(
                    "message",
                    check=pred,
                    timeout=60,
                )
            except asyncio.TimeoutError:
                await msg.edit(content="Timed out.")
                return
            return question["preset_options"][message.content - 1]
        else:
            msg = await member.send(
                f"Question: {question['question']}\nAnswer: \nPlease type your answer."
            )
            try:
                response = await self.bot.wait_for(
                    "message",
                    check=lambda m: m.author == member and m.channel == member.dm_channel,
                    timeout=60,
                )
            except asyncio.TimeoutError:
                await msg.edit(content="Timed out.")
                return
            return response.content

    async def get_image(self, guild: discord.Guild, message: discord.Message):
        # if there are no attachments and the message is not a link, return the message content.
        # if there is an attachment or the content is a image link, save the returned image locally with the filename {message.guild.id}-{message.author.id}.
        # return the filename.

        if not message.attachments and not await self.is_url_image(message.content):
            return
        else:
            image_url = message.attachments[0].url if message.attachments else message.content
            async with aiohttp.ClientSession() as session:
                async with session.get(image_url) as resp:
                    if resp.status != 200:
                        return
                    data = io.BytesIO(await resp.read())
                    filename = f"{guild.id}-{message.author.id}"
                    image = Image.open(data)
                    path = cog_data_path(self) / "pfps/"
                    try:
                        path.mkdir(parents=True, exist_ok=True)
                    except FileExistsError:
                        pass
                    path = path / f"{filename}.png"
                    path.touch()
                    image.save(path, "PNG")
                    return str(path)

    async def is_url_image(self, url: str):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return False
                    return resp.headers["Content-Type"].startswith("image")

        except aiohttp.InvalidURL:
            return False

    @matcherset.command(name="showsettings", aliases=["showsetting", "ss"])
    async def ms_showsettings(self, ctx: commands.Context):
        """Show the settings for the matchmaker cog."""
        settings = await self.config.guild(ctx.guild).all()
        if settings == GUILD_DEFAULTS:
            return await ctx.send("No settings have been configured yet.")
        embed = (
            discord.Embed(
                title="Matcher Settings",
                description="Questions: \n"
                + "\n".join(
                    question["question"]
                    for question in await self.config.guild(ctx.guild).questions()
                ),
                color=await ctx.embed_color(),
            )
            .add_field(
                name="Gender-Status Roles",
                value="\n".join(
                    f"**{gs.replace('_', ' ').title()}**: {getattr(ctx.guild.get_role(role), 'mention', 'not set')}"
                    for gs, role in settings["gender_status_roles"].items()
                ),
                inline=False,
            )
            .add_field(
                name="Bio Channels",
                value="\n".join(
                    f"**{gender} {status}**: {getattr(ctx.guild.get_channel(chan_id), 'mention', 'not set')}"
                    for gender, subdict in settings["bio_channel"].items()
                    for status, chan_id in subdict.items()
                ),
                inline=False,
            )
        )

        await ctx.send(embed=embed)
