from .main import Matcher


def setup(bot):
    bot.add_cog(Matcher(bot))
